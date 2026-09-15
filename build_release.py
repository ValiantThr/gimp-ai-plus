#!/usr/bin/env python3
"""
Build release package for GIMP AI Plugin

Creates a ZIP file containing:
- gimp-ai-plugin/ folder with both required .py files
- Installation instructions
- License

This makes it easy for users to download and install the plugin.
"""

import os
import sys
import shutil
import zipfile
from pathlib import Path
from datetime import datetime

def get_version():
    """Extract version from gimp-ai-plugin.py"""
    plugin_file = Path(__file__).parent / "gimp-ai-plugin.py"
    
    # Try to find version in the file
    try:
        with open(plugin_file, 'r', encoding='utf-8') as f:
            for line in f:
                if 'VERSION' in line and '=' in line:
                    # Extract version string
                    version = line.split('=')[1].strip().strip('"').strip("'")
                    return version
    except Exception:
        pass
    
    # Default version if not found
    return "0.8.0-beta"

def create_release_package():
    """Create a release ZIP package"""
    script_dir = Path(__file__).parent
    version = get_version()
    
    # Create output directory
    output_dir = script_dir / "dist"
    output_dir.mkdir(exist_ok=True)
    
    # Create temporary build directory
    build_dir = output_dir / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()
    
    # Create the plugin subdirectory structure
    plugin_dir = build_dir / "gimp-ai-plugin"
    plugin_dir.mkdir()
    
    print(f"📦 Building GIMP AI Plugin Release v{version}")
    print("=" * 60)
    
    # Required files to include in the plugin folder
    plugin_files = [
        "gimp-ai-plugin.py",
        "coordinate_utils.py",
        "openai_client.py"
    ]
    
    # Copy plugin files
    print("\n📁 Copying plugin files...")
    for filename in plugin_files:
        src = script_dir / filename
        if not src.exists():
            print(f"❌ ERROR: Required file not found: {filename}")
            return False
        
        dst = plugin_dir / filename
        shutil.copy2(src, dst)
        print(f"  ✅ {filename}")
    
    # Documentation files to include in the root
    doc_files = [
        "INSTALL.md",
        "README.md",
        "LICENSE",
        "TROUBLESHOOTING.md",
        "CHANGELOG.md",
        "install_plugin.py"  # Include the automated installer
    ]
    
    print("\n📄 Copying documentation...")
    for filename in doc_files:
        src = script_dir / filename
        if src.exists():
            dst = build_dir / filename
            shutil.copy2(src, dst)
            print(f"  ✅ {filename}")
        else:
            print(f"  ⚠️  {filename} (not found, skipping)")
    
    # Create a quick start file
    quick_start = build_dir / "QUICK_START.txt"
    with open(quick_start, 'w', encoding='utf-8') as f:
        f.write(f"""GIMP AI Plugin v{version} - Quick Start
{"=" * 60}

Thanks for downloading the GIMP AI Plugin!

INSTALLATION OPTIONS:
--------------------

OPTION 1: AUTOMATED INSTALLER (RECOMMENDED)
-------------------------------------------

Simply run the installer script:

   python3 install_plugin.py

The installer will:
• Detect your operating system
• Find your GIMP plugins directory automatically
• Copy files to the correct location
• Set proper permissions
• Show you the next steps

OPTION 2: MANUAL INSTALLATION
------------------------------

1. LOCATE YOUR GIMP PLUGIN FOLDER:
   
   Windows:   %APPDATA%\\GIMP\\3.0\\plug-ins\\
   macOS:     ~/Library/Application Support/GIMP/3.0/plug-ins/
   Linux:     ~/.config/GIMP/3.0/plug-ins/
   
   (Replace 3.0 with 3.1 if you have GIMP 3.1)

2. COPY THE PLUGIN FOLDER:
   
   Copy the entire "gimp-ai-plugin" folder from this archive
   into your GIMP plug-ins directory.
   
   Final structure should look like:
   plug-ins/
   └── gimp-ai-plugin/
       ├── gimp-ai-plugin.py
       ├── coordinate_utils.py
       └── openai_client.py

3. SET PERMISSIONS (Linux/macOS only):
   
   Run this command in a terminal:
   chmod +x ~/.config/GIMP/3.0/plug-ins/gimp-ai-plugin/gimp-ai-plugin.py

AFTER INSTALLATION:
-------------------

1. 🔄 RESTART GIMP completely (quit and reopen)

2. 🔑 CONFIGURE API KEY:
   
   In GIMP: Filters → AI → Settings
   Enter your OpenAI API key (from platform.openai.com)

3. 🎨 TEST THE PLUGIN:

   Try: Filters → AI → Image Generator
   Enter: "blue sky with clouds"

NEED HELP?
----------

📖 See INSTALL.md for detailed step-by-step instructions
🐛 Having issues? Check TROUBLESHOOTING.md
📝 Report bugs: https://github.com/lukaso/gimp-ai/issues

USAGE:
------

After installation, find AI features at: Filters → AI

- 🎨 Inpainting - Fill selections with AI content
- 🖼️ Image Generator - Create images from text
- 🔄 Layer Composite - Blend layers with AI

Enjoy!
""")
    print(f"  ✅ QUICK_START.txt")
    
    # Create the ZIP file
    timestamp = datetime.now().strftime("%Y%m%d")
    zip_filename = f"gimp-ai-plugin-v{version}.zip"
    zip_path = output_dir / zip_filename
    
    print(f"\n📦 Creating ZIP archive...")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add all files from build directory
        for root, dirs, files in os.walk(build_dir):
            for file in files:
                file_path = Path(root) / file
                # Calculate the archive path (relative to build_dir)
                arcname = file_path.relative_to(build_dir)
                zipf.write(file_path, arcname)
                print(f"  ✅ {arcname}")
    
    # Clean up build directory
    shutil.rmtree(build_dir)
    
    # Display results
    file_size = zip_path.stat().st_size / 1024  # Size in KB
    
    print("\n" + "=" * 60)
    print("✅ Release package created successfully!")
    print(f"\n📦 Package: {zip_path}")
    print(f"📊 Size: {file_size:.1f} KB")
    
    # Show what's included
    print(f"\n📋 Package contents:")
    with zipfile.ZipFile(zip_path, 'r') as zipf:
        for info in zipf.filelist:
            print(f"  • {info.filename}")
    
    print("\n🎉 Ready for distribution!")
    print(f"\nUsers can:")
    print(f"1. Download {zip_filename}")
    print(f"2. Extract the ZIP file")
    print(f"3. Run: python3 install_plugin.py (automated)")
    print(f"   OR")
    print(f"   Copy the 'gimp-ai-plugin' folder to their GIMP plug-ins directory (manual)")
    print(f"4. Restart GIMP")
    
    return True

def main():
    """Main entry point"""
    try:
        success = create_release_package()
        return 0 if success else 1
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
