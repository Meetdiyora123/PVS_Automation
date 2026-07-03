import re
import csv

INPUT_FILE = "packs_txt"
OUTPUT_FILE = "gsutil_command.sh"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    content = f.read()

# Extract pack_id and image name pairs
matches = re.findall(
    r'"(\d+)","(\d{4}-\d{2}-\d{2})_P\d+[^"]*"',
    content
)

if not matches:
    print("No pack/image pairs found!")
    exit(1)

lines = ["gsutil -m cp \\"]

for pack_id, date in matches:
    lines.append(
        f'"gs://dp-v4-pcp/slot_images/{date}_P{pack_id}*" \\'
    )

# Add destination folder
lines.append(".")

command = "\n".join(lines)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write(command)

print(f"Generated command saved to {OUTPUT_FILE}")
print(f"Total packs found: {len(matches)}")

#  sdsds testing git workflow