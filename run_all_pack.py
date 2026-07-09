import subprocess
import sys

PACKS = [
    164285, 164347, 164352, 164367, 164429, 163895, 163924, 163965, 163984,
    164052, 164063, 164064, 164091, 164185, 164187, 164215, 164243, 163969,
    164137, 164138, 164142, 164145, 164146, 164161, 164205, 164211, 164004,
    164043, 164046, 164094, 164101, 164137, 164142, 164169, 163160, 163431,
    163437, 163438, 163447, 163455, 163461, 163463, 163465, 163471, 163486,
    163500, 163515, 163519, 163523, 163525, 163526, 163427, 163472, 163479,
    163481, 163491, 163609, 163703, 163704, 163726, 164241, 163990, 163995,
	163997, 164023, 164031
]

failed = []

for i, pack in enumerate(PACKS, 1):
    print(f"\n[{i}/{len(PACKS)}] Running pack {pack}...")
    result = subprocess.run(
        [sys.executable, "pvs_automation.py", "--test-pack", str(pack)]
    )
    if result.returncode != 0:
        print(f"  !! Pack {pack} FAILED (exit code {result.returncode})")
        failed.append(pack)
    else:
        print(f"  ✓ Pack {pack} done")

print(f"\n========== DONE ==========")
print(f"Passed : {len(PACKS) - len(failed)}/{len(PACKS)}")
print(f"Failed : {len(failed)}")
if failed:
    print(f"Failed packs: {failed}")