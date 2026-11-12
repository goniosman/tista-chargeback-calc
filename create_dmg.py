#!/usr/bin/env python3
"""
Builds Chargeback_Calculator_Intel.dmg and Chargeback_Calculator_macOS.zip
for Intel and Apple Silicon Macs.
"""
import os
import re
import shutil
import zipfile
import pycdlib

def main():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    macos_dir = os.path.join(root_dir, "macOS")
    output_dmg = os.path.join(root_dir, "Chargeback_Calculator_Intel.dmg")
    output_zip = os.path.join(root_dir, "Chargeback_Calculator_Intel_Mac.zip")

    print("[*] Normalizing line endings to LF for macOS...")
    for root, dirs, files in os.walk(macos_dir):
        for f in files:
            if f.endswith(('.command', '.sh', '.py', 'Chargeback_Calculator', '.plist', '.txt', '.html', '.js', '.css', '.json')):
                fp = os.path.join(root, f)
                with open(fp, 'rb') as rf:
                    c = rf.read()
                norm = c.replace(b'\r\n', b'\n')
                if norm != c:
                    with open(fp, 'wb') as wf:
                        wf.write(norm)

    print("[*] Building Chargeback_Calculator_Intel.dmg with pycdlib...")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge='1.12', vol_ident='ChargebackCalc')

    counter = [0]
    def sanitize_iso_name(name, is_dir=False):
        counter[0] += 1
        ext = ''
        base = name
        if not is_dir and '.' in name:
            parts = name.rsplit('.', 1)
            base = parts[0]
            ext = '.' + re.sub(r'[^A-Z0-9]', '', parts[1].upper())[:3]
        clean_base = re.sub(r'[^A-Z0-9]', '', base.upper())[:4]
        if not clean_base:
            clean_base = 'ITEM'
        return f'{clean_base}{counter[0]:04d}{ext}'

    def add_dir_to_iso(src_dir, iso_dir, joliet_dir):
        for item in sorted(os.listdir(src_dir)):
            s_path = os.path.join(src_dir, item)
            is_dir = os.path.isdir(s_path)
            iso_name = sanitize_iso_name(item, is_dir)
            i_path = f'{iso_dir}/{iso_name}'

            j_safe = re.sub(r'[^\w\s.-]', '_', item)
            if not j_safe.strip('_'):
                j_safe = 'INSTRUCTIONS_BN.txt'
            # Joliet name limit 64 chars
            if len(j_safe) > 50:
                j_safe = j_safe[:46] + ('.' + j_safe.rsplit('.', 1)[-1] if '.' in j_safe else '')
            j_path = f'{joliet_dir}/{j_safe}'

            if is_dir:
                iso.add_directory(i_path, joliet_path=j_path, rr_name=item)
                add_dir_to_iso(s_path, i_path, j_path)
            else:
                iso.add_file(s_path, f'{i_path};1', joliet_path=j_path, rr_name=item)

    add_dir_to_iso(macos_dir, '', '')
    iso.write(output_dmg)
    iso.close()

    dmg_size = os.path.getsize(output_dmg)
    print(f"[✓] Created DMG: {output_dmg} ({dmg_size:,} bytes)")

    print("[*] Creating Zip package: Chargeback_Calculator_Intel_Mac.zip...")
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(macos_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, macos_dir)
                zf.write(full_path, rel_path)
    zip_size = os.path.getsize(output_zip)
    print(f"[✓] Created ZIP: {output_zip} ({zip_size:,} bytes)")

if __name__ == "__main__":
    main()
