"""
Run this from your DWG Viewer project folder to diagnose issues:
    python diagnose.py
"""
import sys, os, shutil
from pathlib import Path

print("=" * 60)
print("DWG Viewer Diagnostics")
print("=" * 60)

print(f"\nPython: {sys.version}")
print(f"Running from: {Path(__file__).parent.resolve()}")

# Packages
print("\n--- Packages ---")
for pkg in ["ezdxf", "PyQt6"]:
    try:
        m = __import__(pkg)
        print(f"  {pkg}: {getattr(m, '__version__', 'installed')}")
    except ImportError:
        print(f"  {pkg}: NOT INSTALLED")

# ODA search
print("\n--- ODA File Converter search ---")
found_oda = None

in_path = shutil.which("ODAFileConverter") or shutil.which("ODAFileConverter.exe")
if in_path:
    print(f"  Found in PATH: {in_path}")
    found_oda = in_path

for base in [r"C:\Program Files\ODA", r"C:\Program Files (x86)\ODA"]:
    if os.path.isdir(base):
        print(f"  Scanning {base}:")
        for entry in sorted(os.scandir(base), key=lambda e: e.name, reverse=True):
            candidate = os.path.join(entry.path, "ODAFileConverter.exe")
            exists = os.path.isfile(candidate)
            print(f"    {'FOUND' if exists else 'missing'}: {candidate}")
            if exists and not found_oda:
                found_oda = candidate
    else:
        print(f"  Not present: {base}")

if not found_oda:
    print("\n  *** ODA File Converter NOT FOUND ***")
    print("  Download: https://www.opendesign.com/guestfiles/oda_file_converter")
else:
    print(f"\n  Using: {found_oda}")
    import tempfile, subprocess
    tmp_in  = Path(tempfile.mkdtemp(prefix="dwgtest_in_"))
    tmp_out = Path(tempfile.mkdtemp(prefix="dwgtest_out_"))
    fake = tmp_in / "test.DWG"
    fake.write_bytes(b"AC1032" + b"\x00" * 200)
    cmd = [found_oda, str(tmp_in), str(tmp_out), "ACAD2018", "DXF", "0", "1"]
    print(f"\n--- Test run ---")
    print(f"  cmd: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"  return code: {r.returncode}")
        if r.stdout.strip(): print(f"  stdout: {r.stdout.strip()}")
        if r.stderr.strip(): print(f"  stderr: {r.stderr.strip()}")
        out = list(tmp_out.iterdir())
        print(f"  output files: {[f.name for f in out]}")
    except Exception as e:
        print(f"  ERROR: {e}")
    finally:
        shutil.rmtree(tmp_in, ignore_errors=True)
        shutil.rmtree(tmp_out, ignore_errors=True)

# Check which converter.py version is present
print("\n--- converter.py version check ---")
src_conv = Path(__file__).parent / "src" / "converter.py"
if src_conv.exists():
    text = src_conv.read_text()
    print(f"  Has ODA logic:      {'YES' if 'NeedODAConverter' in text else 'NO  <-- needs update'}")
    print(f"  Has recover import: {'YES' if 'ezdxf_recover' in text else 'NO  <-- needs update'}")
else:
    print("  src/converter.py not found in this folder")
    print(f"  (looking in: {src_conv})")

print("\n" + "=" * 60)
input("\nPress Enter to exit...")
