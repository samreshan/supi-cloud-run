import torch
from omnivoice import OmniVoice

def preload():
    print("Preloading k2-fsa/OmniVoice model to cache...")
    # Using device_map="cpu" to run on the CPU during the Docker build process,
    # downloading model weights and dependencies.
    model = OmniVoice.from_pretrained(
        "k2-fsa/OmniVoice",
        device_map="cpu",
        dtype=torch.float32,
        load_asr=True
    )
    print("Model preload complete. Cache is successfully initialized.")

if __name__ == "__main__":
    preload()
