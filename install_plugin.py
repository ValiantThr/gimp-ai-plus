#!/usr/bin/env python3
"""
Automated installer for GIMP AI Plugin
Works on Windows, macOS, and Linux

This script will:
1. Detect your operating system
2. Find your GIMP plugins directory
3. Copy the plugin files to the correct location
4. Set appropriate permissions
5. Provide next steps

Usage:
    python3 install_plugin.py
"""

import os
import sys
import platform
import shutil
import re
from pathlib import Path

# Every file the plugin needs at runtime. Keep this in step with
# build_release.py's plugin_files list.
PLUGIN_FILES = [
    "gimp-ai-plugin.py",
    "coordinate_utils.py",
    "openai_client.py",
]


def parse_version(version_str):
    """Parse version string like '3.0' or '3.1' into tuple (major, minor, patch)"""
    try:
        parts = version_str.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return None


def is_version_compatible(version_str):
    """Check if version is GIMP 3.x"""
    parsed = parse_version(version_str)
    if not parsed:
        return False

    major, minor, patch = parsed

    # Must be GIMP 3.x
    return major == 3


def is_stable_version(version_str):
    """Check if version is stable (even minor version number)"""
    parsed = parse_version(version_str)
    if parsed:
        major, minor, patch = parsed
        return minor % 2 == 0  # Stable versions have even minor numbers
    return False


def find_all_gimp_versions(base_path):
    """Find all GIMP 3.X version directories in the given base path"""
    if not os.path.exists(base_path):
        return []

    versions = []

    try:
        for item in os.listdir(base_path):
            # Skip invalid directory names
            if item == "3.00":
                continue

            # Match GIMP 3.X version directories (3.0, 3.1, 3.2, etc.)
            if re.match(r"^3\.\d+$", item):
                version_path = os.path.join(base_path, item)
                if os.path.isdir(version_path):
                    if is_version_compatible(item):
                        versions.append(item)
    except PermissionError:
        pass

    return versions


def sort_versions(versions):
    """Sort versions by version number, with stable versions prioritized"""

    def version_key(v):
        parsed = parse_version(v)
        if not parsed:
            return (0, 1, 0, 0)  # Put unparseable versions last
        major, minor, patch = parsed
        # Sort by: major (desc), stable first (0=stable, 1=dev), minor (desc), patch (desc)
        # For stable versions: higher minor is better (3.2 > 3.0)
        # For dev versions: higher minor is better (3.3 > 3.1)
        is_dev = 1 if minor % 2 == 1 else 0
        return (-major, is_dev, -minor, -patch)

    return sorted(versions, key=version_key)


def prompt_user_choice(versions):
    """Prompt user to choose from multiple GIMP versions"""
    print()
    print("🔍 Multiple GIMP versions detected:")
    print()

    # Sort and display with stable versions marked
    sorted_versions = sort_versions(versions)

    for i, version in enumerate(sorted_versions, 1):
        stable_marker = " (stable)" if is_stable_version(version) else " (development)"
        print(f"  {i}. GIMP {version}{stable_marker}")

    print()
    default_version = sorted_versions[0]
    print(f"Default: GIMP {default_version} (recommended)")
    print()

    while True:
        try:
            choice = input(
                f"Select version [1-{len(sorted_versions)}] or press Enter for default: "
            ).strip()

            if not choice:
                return default_version

            choice_num = int(choice)
            if 1 <= choice_num <= len(sorted_versions):
                return sorted_versions[choice_num - 1]
            else:
                print(f"Please enter a number between 1 and {len(sorted_versions)}")
        except ValueError:
            print("Please enter a valid number or press Enter")
        except KeyboardInterrupt:
            print("\n❌ Installation cancelled")
            sys.exit(1)


def get_gimp_plugins_dir():
    """Get the GIMP plugins directory for the current platform"""
    system = platform.system()

    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            gimp_base = os.path.join(appdata, "GIMP")
            versions = find_all_gimp_versions(gimp_base)

            if versions:
                if len(versions) == 1:
                    chosen_version = versions[0]
                else:
                    chosen_version = prompt_user_choice(versions)
                return os.path.join(gimp_base, chosen_version, "plug-ins")

            # No compatible versions found
            return None

    elif system == "Darwin":  # macOS
        home = os.path.expanduser("~")
        gimp_base = os.path.join(home, "Library", "Application Support", "GIMP")
        versions = find_all_gimp_versions(gimp_base)

        if versions:
            if len(versions) == 1:
                chosen_version = versions[0]
            else:
                chosen_version = prompt_user_choice(versions)
            return os.path.join(gimp_base, chosen_version, "plug-ins")

        # No compatible versions found
        return None

    elif system == "Linux":
        home = os.path.expanduser("~")

        # Check for Flatpak installation first
        flatpak_base = os.path.join(
            home, ".var", "app", "org.gimp.GIMP", "config", "GIMP"
        )
        versions = find_all_gimp_versions(flatpak_base)

        if versions:
            if len(versions) == 1:
                chosen_version = versions[0]
            else:
                chosen_version = prompt_user_choice(versions)
            return os.path.join(flatpak_base, chosen_version, "plug-ins")

        # Check standard installation
        gimp_base = os.path.join(home, ".config", "GIMP")
        versions = find_all_gimp_versions(gimp_base)

        if versions:
            if len(versions) == 1:
                chosen_version = versions[0]
            else:
                chosen_version = prompt_user_choice(versions)
            return os.path.join(gimp_base, chosen_version, "plug-ins")

        # No compatible versions found
        return None

    return None


def find_plugin_files():
    """Find the plugin files in the current directory or parent directory"""
    current_dir = Path(__file__).parent

    required_files = PLUGIN_FILES

    # Check current directory
    all_found = all((current_dir / f).exists() for f in required_files)
    if all_found:
        return current_dir

    # Check if we're in a subdirectory (e.g., extracted from ZIP)
    # Look for gimp-ai-plugin subdirectory
    plugin_subdir = current_dir / "gimp-ai-plugin"
    if plugin_subdir.exists():
        all_found = all((plugin_subdir / f).exists() for f in required_files)
        if all_found:
            return plugin_subdir

    return None


def check_existing_installation(plugin_dest_dir):
    """Check if plugin is already installed in the destination directory"""
    if not os.path.exists(plugin_dest_dir):
        return False

    # Check if the main plugin file exists
    plugin_file = os.path.join(plugin_dest_dir, "gimp-ai-plugin.py")
    return os.path.exists(plugin_file)


def prompt_overwrite():
    """Prompt user whether to overwrite existing installation"""
    print("⚠️  An existing installation was detected.")
    print()

    while True:
        try:
            choice = input(
                "Do you want to overwrite it? [y/N]: "
            ).strip().lower()

            if not choice or choice == "n":
                return False
            elif choice == "y":
                return True
            else:
                print("Please enter 'y' for yes or 'n' for no")
        except KeyboardInterrupt:
            print("\n❌ Installation cancelled")
            sys.exit(1)


def install_plugin():
    """Install the plugin to GIMP plugins directory"""

    print("=" * 60)
    print("   GIMP AI Plugin - Automated Installer")
    print("=" * 60)
    print()

    # Detect OS
    system = platform.system()
    os_name = {"Windows": "🪟 Windows", "Darwin": "🍎 macOS", "Linux": "🐧 Linux"}.get(
        system, system
    )

    print(f"Detected OS: {os_name}")
    print()

    # Find plugin files
    print("📁 Looking for plugin files...")
    source_dir = find_plugin_files()

    if not source_dir:
        print("❌ ERROR: Could not find plugin files!")
        print()
        print("Please make sure you have these files:")
        for name in PLUGIN_FILES:
            print(f"  • {name}")
        print()
        print("They should be in the same directory as this installer.")
        return False

    source_files = [source_dir / name for name in PLUGIN_FILES]

    print(f"✅ Found plugin files in: {source_dir}")
    print()

    # Get GIMP plugins directory
    print("🔍 Finding GIMP plugins directory...")
    plugins_dir = get_gimp_plugins_dir()

    if not plugins_dir:
        print("❌ ERROR: Could not determine GIMP plugins directory")
        print()
        print("Please install GIMP 3.0.4+ first, or install manually:")
        print("See INSTALL.md for manual installation instructions.")
        return False

    print(f"✅ GIMP plugins directory: {plugins_dir}")
    print()

    # Create plugins directory if it doesn't exist
    try:
        os.makedirs(plugins_dir, exist_ok=True)
    except PermissionError:
        print("❌ ERROR: Permission denied creating plugins directory")
        print(f"   Path: {plugins_dir}")
        print()
        print("Try running as administrator/sudo, or install manually.")
        return False

    # Check for existing installation
    plugin_dest_dir = os.path.join(plugins_dir, "gimp-ai-plugin")
    is_update = check_existing_installation(plugin_dest_dir)

    if is_update:
        if not prompt_overwrite():
            print()
            print("❌ Installation cancelled by user")
            print()
            print("No changes were made to your existing installation.")
            return False
        print()
        print("📝 Overwriting existing installation...")
    else:
        print(f"📂 Creating plugin directory: {plugin_dest_dir}")

    # Create plugin subdirectory
    try:
        os.makedirs(plugin_dest_dir, exist_ok=True)
        if not is_update:
            print("✅ Plugin directory created")
    except PermissionError:
        print("❌ ERROR: Permission denied creating plugin subdirectory")
        return False

    print()

    # Copy plugin files
    print("📋 Copying plugin files...")

    try:
        dest_plugin = os.path.join(plugin_dest_dir, "gimp-ai-plugin.py")

        for source in source_files:
            shutil.copy2(source, os.path.join(plugin_dest_dir, source.name))
            print(f"  ✅ {source.name}")

    except (IOError, PermissionError) as e:
        print(f"❌ ERROR copying files: {e}")
        return False

    print()

    # Set executable permissions on Unix-like systems
    if platform.system() in ["Darwin", "Linux"]:
        print("🔐 Setting file permissions...")
        try:
            os.chmod(dest_plugin, 0o755)
            print("✅ Plugin made executable")
        except PermissionError:
            print("⚠️  Warning: Could not make plugin executable")
            print("   You may need to run: chmod +x " + dest_plugin)

    print()
    print("=" * 60)
    if is_update:
        print("✅ Plugin updated successfully!")
    else:
        print("✅ Installation completed successfully!")
    print("=" * 60)

    return True


def print_next_steps():
    """Print instructions for next steps"""
    print()
    print("📋 NEXT STEPS:")
    print()
    print("1. 🔄 Restart GIMP completely (quit and reopen)")
    print()
    print("2. 🔍 Check for the AI menu:")
    print("   In GIMP: Filters → AI")
    print()
    print("3. 🔑 Configure your API key:")
    print("   a. Get an OpenAI API key from: https://platform.openai.com")
    print("   b. In GIMP: Filters → AI → Settings")
    print("   c. Paste your API key (starts with 'sk-')")
    print("   d. Click OK")
    print()
    print("4. 🎨 Test the plugin:")
    print("   Try: Filters → AI → Image Generator")
    print("   Enter a prompt like: 'blue sky with clouds'")
    print()
    print("=" * 60)
    print()
    print("📖 For more help:")
    print("   • Installation guide: INSTALL.md")
    print("   • Troubleshooting: TROUBLESHOOTING.md")
    print("   • Report issues: https://github.com/lukaso/gimp-ai/issues")
    print()
    print("🎉 Enjoy using GIMP AI Plugin!")


def main():
    """Main entry point"""
    try:
        success = install_plugin()

        if success:
            print_next_steps()
            return 0
        else:
            print()
            print("Installation failed. Please try manual installation:")
            print("See INSTALL.md for detailed instructions.")
            return 1

    except KeyboardInterrupt:
        print("\n\n❌ Installation cancelled by user")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        print()
        print("Please try manual installation. See INSTALL.md for instructions.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
