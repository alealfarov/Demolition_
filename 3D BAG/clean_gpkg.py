from pathlib import Path
import gzip
import shutil
import os

ROOT = Path(r"C:\Internship_DF\Thesis\3D BAG\raw\gpkg")


def valid_gpkg(path: Path):
    """
    Check whether the file looks like a valid SQLite/GeoPackage database.
    """
    try:
        if not path.is_file():
            return False

        if path.stat().st_size == 0:
            return False

        with open(path, "rb") as f:
            header = f.read(16)

        return header.startswith(b"SQLite format 3")

    except Exception:
        return False


print("=" * 70)
print("3DBAG GPKG CLEANUP + EXTRACTION")
print("=" * 70)

# ==========================================================
# STEP 1
# Fix previously extracted folders:
#
# 10-760-906.gpkg\
#     10-760-906.gpkg
#
# becomes:
#
# 10-760-906.gpkg
# ==========================================================

print("\nSTEP 1 - Fixing already extracted .gpkg folders...\n")

gpkg_folders = [
    p for p in ROOT.iterdir()
    if p.is_dir() and p.name.lower().endswith(".gpkg")
]

fixed_folders = 0
folder_errors = 0

for folder in gpkg_folders:

    expected_file = folder / folder.name

    print(f"Checking folder: {folder.name}")

    if not expected_file.exists():
        print("  WARNING: expected GPKG not found inside.")
        print("  Folder left untouched.\n")
        folder_errors += 1
        continue

    if not valid_gpkg(expected_file):
        print("  WARNING: internal file does not look like a valid GeoPackage.")
        print("  Folder left untouched.\n")
        folder_errors += 1
        continue

    # Make sure there is no unexpected content
    contents = list(folder.iterdir())

    if len(contents) != 1:
        print(f"  WARNING: folder contains {len(contents)} items.")
        print("  Folder left untouched for safety.\n")
        folder_errors += 1
        continue

    # Temporary name because the folder currently occupies
    # the desired final path
    temp_file = ROOT / (folder.name + ".moving")

    try:
        print(f"  Moving {expected_file.name} to main folder...")

        shutil.move(str(expected_file), str(temp_file))

        # Folder should now be empty
        folder.rmdir()

        # Rename temporary file to the actual .gpkg name
        temp_file.rename(ROOT / folder.name)

        fixed_folders += 1
        print("  OK\n")

    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Please inspect this item manually.\n")
        folder_errors += 1


# ==========================================================
# STEP 2
# Extract remaining .gpkg.gz files
# ==========================================================

print("\nSTEP 2 - Extracting remaining .gpkg.gz files...\n")

gz_files = list(ROOT.glob("*.gpkg.gz"))

print(f"Found {len(gz_files)} compressed GeoPackages.\n")

extracted = 0
already_existing = 0
extract_errors = 0


for i, gz_path in enumerate(gz_files, start=1):

    output_path = gz_path.with_suffix("")
    temp_path = Path(str(output_path) + ".part")

    print(f"[{i}/{len(gz_files)}] {gz_path.name}")

    # ------------------------------------------------------
    # If final GPKG already exists
    # ------------------------------------------------------

    if output_path.exists():

        if output_path.is_file() and valid_gpkg(output_path):

            print("  GeoPackage already exists and is valid.")
            print("  Removing redundant .gz archive.")

            gz_path.unlink()

            already_existing += 1
            print("  OK\n")
            continue

        else:
            print("  WARNING: target exists but is not a valid GPKG.")
            print("  Archive kept for safety.\n")

            extract_errors += 1
            continue

    # ------------------------------------------------------
    # Extract to temporary file first
    # ------------------------------------------------------

    try:

        print(f"  Extracting -> {output_path.name}")

        with gzip.open(gz_path, "rb") as source:
            with open(temp_path, "wb") as destination:
                shutil.copyfileobj(source, destination)

        # --------------------------------------------------
        # Validate temporary extraction
        # --------------------------------------------------

        if not valid_gpkg(temp_path):

            print("  ERROR: extracted file failed GeoPackage validation.")
            print("  Original archive will be kept.")

            if temp_path.exists():
                temp_path.unlink()

            extract_errors += 1
            print()
            continue

        # --------------------------------------------------
        # Rename final file
        # --------------------------------------------------

        temp_path.rename(output_path)

        # Final check
        if valid_gpkg(output_path):

            gz_path.unlink()

            extracted += 1

            size_mb = output_path.stat().st_size / (1024 * 1024)

            print(f"  Valid GeoPackage: {size_mb:.2f} MB")
            print("  Original .gz deleted.")
            print("  OK\n")

        else:

            print("  ERROR during final validation.")
            print("  Original archive kept.\n")

            extract_errors += 1

    except Exception as e:

        print(f"  ERROR: {e}")
        print("  Original archive kept.")

        if temp_path.exists():
            temp_path.unlink()

        extract_errors += 1
        print()


# ==========================================================
# FINAL SUMMARY
# ==========================================================

print("\n" + "=" * 70)
print("FINISHED")
print("=" * 70)

print(f"""
Already-extracted folders fixed : {fixed_folders}
New .gz files extracted         : {extracted}
Existing valid GPKGs found      : {already_existing}

Folder problems                 : {folder_errors}
Extraction problems             : {extract_errors}
""")

remaining_gz = list(ROOT.glob("*.gpkg.gz"))
final_gpkg = [p for p in ROOT.glob("*.gpkg") if p.is_file()]
remaining_gpkg_folders = [
    p for p in ROOT.glob("*.gpkg") if p.is_dir()
]

print(f"Final GeoPackage files : {len(final_gpkg)}")
print(f"Remaining .gz files    : {len(remaining_gz)}")
print(f"Remaining GPKG folders : {len(remaining_gpkg_folders)}")

print("=" * 70)

if not remaining_gz and not remaining_gpkg_folders:
    print("SUCCESS: folder is clean.")
    print("Only actual .gpkg files remain.")
else:
    print("Some items were kept because they require inspection.")

print("=" * 70)