from pathlib import Path
import tkinter as tk
from tkinter import filedialog
from PIL import Image

# Resolves the exact folder where this script lives
PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_ICO = PROJECT_ROOT / "app_icon.ico"

def get_source_image() -> Path | None:
    # 1. Check if a standard logo image is already placed in the project root
    for default_name in ("logo.png", "icon.png", "logo.jpg", "app_icon.png"):
        candidate = PROJECT_ROOT / default_name
        if candidate.exists():
            print(f"Auto-detected image: {candidate.name}")
            return candidate

    # 2. If not found, open a Windows file browser to pick the image
    root = tk.Tk()
    root.withdraw()  # Hide the main Tkinter window
    
    file_path = filedialog.askopenfilename(
        title="Select Image for App Icon",
        filetypes=[("Image Files", "*.png *.jpg *.jpeg *.webp *.bmp")]
    )
    return Path(file_path) if file_path else None

def create_ico():
    src = get_source_image()
    if not src or not src.exists():
        print("No image selected. Aborted.")
        return

    try:
        img = Image.open(src)
        
        # Ensure image has an alpha channel for clean transparency
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # Standard Windows multi-resolution icon sizes
        resolutions = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        
        img.save(
            OUTPUT_ICO,
            format="ICO",
            sizes=resolutions
        )
        print(f"✓ Generated high-DPI icon: {OUTPUT_ICO}")
    except Exception as e:
        print(f"Failed to generate icon: {e}")

if __name__ == "__main__":
    create_ico()