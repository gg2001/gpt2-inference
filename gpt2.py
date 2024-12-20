import os
import argparse
import torch
from torch.nn.functional import gelu, layer_norm, softmax
from torch.utils import cpp_extension
from transformers import GPT2Model, AutoTokenizer
from typing import TypedDict


os.environ["TORCH_CUDA_ARCH_LIST"] = "8.0"
MODEL_NAME = "gpt2"

if not torch.cuda.is_available():
    raise ValueError("CUDA is not available")

kernels = cpp_extension.load(
    name="flash_attention", sources=["main.cpp", "main.cu"], extra_cuda_cflags=["-O2"]
)


device = torch.device("cuda")
model = GPT2Model.from_pretrained(MODEL_NAME).to(device)
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, clean_up_tokenization_spaces=True)

# Hyperparameters
cfg = model.config.to_dict()
vocab_size: int = cfg["vocab_size"]  # 50257, token dictionary size
n_positions: int = cfg["n_positions"]  # 1024, "context length"
n_embd: int = cfg["n_embd"]  # 768, token embedding dimensions
n_head: int = cfg["n_head"]  # 12, attention heads per block
n_layer: int = cfg["n_layer"]  # 12, number of transformer blocks
d_mlp: int = n_embd * 4  # 3072, MLP hidden layer dimensions
d_head: int = n_embd // n_head  # 64, dimensions of each attention head


# Parameters
class Attention(TypedDict):
    c_attn_weight: torch.Tensor  # (768 = 64 * 12, 2304 = 768 * 3)
    c_attn_bias: torch.Tensor  # (768 * 3,)
    c_proj_weight: torch.Tensor  # (768, 768)
    c_proj_bias: torch.Tensor  # (768,)


class MLP(TypedDict):
    c_fc_weight: torch.Tensor  # (768, 3072)
    c_fc_bias: torch.Tensor  # (3072,)
    c_proj_weight: torch.Tensor  # (3072, 768)
    c_proj_bias: torch.Tensor  # (768,)


class LayerNorm(TypedDict):
    weight: torch.Tensor  # (768,)
    bias: torch.Tensor  # (768,)


class Block(TypedDict):
    ln_1: LayerNorm  # layer norm 1
    attn: Attention  # multi-head self-attention
    ln_2: LayerNorm  # layer norm 2
    mlp: MLP  # Feed-forward


state_dict = model.state_dict()
wte: torch.Tensor = state_dict["wte.weight"].to(device)  # (50257, 768)
wpe: torch.Tensor = state_dict["wpe.weight"].to(device)  # (1024, 768)
blocks: list[Block] = []
for i in range(n_layer):
    blocks.append(
        {
            "ln_1": {
                "weight": state_dict[f"h.{i}.ln_1.weight"].to(device),
                "bias": state_dict[f"h.{i}.ln_1.bias"].to(device),
            },
            "attn": {
                "c_attn_weight": state_dict[f"h.{i}.attn.c_attn.weight"].to(device),
                "c_attn_bias": state_dict[f"h.{i}.attn.c_attn.bias"].to(device),
                "c_proj_weight": state_dict[f"h.{i}.attn.c_proj.weight"].to(device),
                "c_proj_bias": state_dict[f"h.{i}.attn.c_proj.bias"].to(device),
            },
            "ln_2": {
                "weight": state_dict[f"h.{i}.ln_2.weight"].to(device),
                "bias": state_dict[f"h.{i}.ln_2.bias"].to(device),
            },
            "mlp": {
                "c_fc_weight": state_dict[f"h.{i}.mlp.c_fc.weight"].to(device),
                "c_fc_bias": state_dict[f"h.{i}.mlp.c_fc.bias"].to(device),
                "c_proj_weight": state_dict[f"h.{i}.mlp.c_proj.weight"].to(device),
                "c_proj_bias": state_dict[f"h.{i}.mlp.c_proj.bias"].to(device),
            },
        }
    )
ln_f: LayerNorm = {
    "weight": state_dict["ln_f.weight"].to(device),
    "bias": state_dict["ln_f.bias"].to(device),
}


def forward(input: torch.Tensor) -> torch.Tensor:
    batch_size, token_len = input.shape

    # token embeddings + position embeddings
    x = (
        wte[input]  # (batch_size, token_len, n_embd)
        + wpe[
            torch.arange(token_len, device=device)  # [0, 1, 2, ..., token_len-1]
        ]  # (token_len, n_embd)
    )  # (batch_size, token_len, n_embd)

    # residual stream
    for block in blocks:
        ########################################
        # layer norm 1
        ########################################
        ln_1 = layer_norm(
            x, (n_embd,), weight=block["ln_1"]["weight"], bias=block["ln_1"]["bias"]
        )  # (batch_size, token_len, n_embd)

        ########################################
        # multi-head self-attention using flash attention
        ########################################
        heads = block["attn"]

        # query, key, value
        qkv = (
            ln_1 @ heads["c_attn_weight"] + heads["c_attn_bias"]
        )  # (batch_size, token_len, n_embd * 3)
        q, k, v = (
            qkv[..., :n_embd],
            qkv[..., n_embd : 2 * n_embd],
            qkv[..., 2 * n_embd :],
        )  # (batch_size, token_len, n_embd)

        # separate the heads
        q = q.view(batch_size, token_len, n_head, d_head).transpose(
            1, 2
        )  # (batch_size, n_head, token_len, d_head)
        k = k.view(batch_size, token_len, n_head, d_head).transpose(1, 2)
        v = v.view(batch_size, token_len, n_head, d_head).transpose(1, 2)

        # Use flash attention kernel
        attn_output = kernels.flash_attention(
            q, k, v
        )  # (batch_size, n_head, token_len, d_head)

        # merge heads + linear layer
        concat_heads = attn_output.transpose(1, 2).reshape(
            batch_size, token_len, n_embd
        )  # (batch_size, token_len, n_embd)
        attn_output = (
            concat_heads @ heads["c_proj_weight"] + heads["c_proj_bias"]
        )  # (batch_size, token_len, n_embd)

        ########################################
        # residual connection 1 + layer norm 2
        ########################################
        x = x + attn_output  # (batch_size, token_len, n_embd)
        ln_2 = layer_norm(
            x, (n_embd,), weight=block["ln_2"]["weight"], bias=block["ln_2"]["bias"]
        )

        ########################################
        # mlp
        ########################################
        mlp = block["mlp"]

        # hidden layer
        mlp_output = (
            ln_2 @ mlp["c_fc_weight"] + mlp["c_fc_bias"]
        )  # (batch_size, token_len, d_mlp)
        mlp_output = gelu(mlp_output)
        # output layer
        mlp_output = (
            mlp_output @ mlp["c_proj_weight"] + mlp["c_proj_bias"]
        )  # (batch_size, token_len, n_embd)

        ########################################
        # residual connection 2
        ########################################
        x = x + mlp_output

    # final layer norm
    x = layer_norm(
        x, (n_embd,), weight=ln_f["weight"], bias=ln_f["bias"]
    )  # (batch_size, token_len, n_embd)

    # unembed layer = transpose of the embedding layer
    logits = x @ wte.T  # (batch_size, token_len, vocab_size)

    return logits


def generate(input: str, num_tokens: int, stream: bool = False) -> str:
    print(input)
    tokens = torch.tensor([[tokenizer.bos_token_id]], dtype=torch.int64, device=device)
    if input != "":
        tokenized: torch.Tensor = tokenizer(input, return_tensors="pt").input_ids.to(
            device
        )  # (1, token_len)
        tokens = torch.cat([tokens, tokenized], dim=1)
    new_tokens = torch.empty(0, dtype=torch.int64, device=device)

    for _ in range(num_tokens):
        # Ensure the tokens fit in our context window
        with torch.no_grad():
            logits = forward(tokens[:, -n_positions:])  # (1, token_len, vocab_size)

        # Convert logits to probabilities
        logits = logits[0, -1, :]  # (vocab_size,)
        logits = logits - logits.max()  # Subtract max for numerical stability
        probs = softmax(logits, dim=-1)  # (vocab_size,)
        if torch.isnan(probs).any() or torch.isinf(probs).any():
            break
        # Sample from the distribution
        next_token = torch.multinomial(probs, num_samples=1)  # (1,)

        # Append the next token to the tokens tensor
        tokens = torch.cat(
            [tokens, next_token.unsqueeze(0)], dim=1
        )  # (1, token_len + 1)
        new_tokens = torch.cat([new_tokens, next_token], dim=0)

        if stream:
            print(tokenizer.decode(next_token, skip_special_tokens=True), end="")

        # Stop if we generate an EOS token
        if next_token.item() == tokenizer.eos_token_id:
            break

    if stream:
        print()

    return tokenizer.decode(new_tokens, skip_special_tokens=True)


if __name__ == "__main__":
    default_num_tokens = 50

    parser = argparse.ArgumentParser(description="GPT-2")
    parser.add_argument(
        "--prompt", type=str, required=True, help="Input prompt for text generation"
    )
    parser.add_argument(
        "--tokens",
        type=int,
        default=default_num_tokens,
        help=f"Number of tokens to generate (default: {default_num_tokens})",
    )

    args = parser.parse_args()
    print(args.prompt)

    generate(args.prompt, args.tokens, stream=True)
