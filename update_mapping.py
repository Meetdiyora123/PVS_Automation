import pandas as pd

# Input file
input_file = "Untitled spreadsheet.xlsx"
output_file = "updated_mapping.xlsx"

# Read Excel
df = pd.read_excel(input_file)

# Create mapping: BB -> id_B
bb_to_idb = (
    df[['BB', 'id_B']]
    .dropna(subset=['BB'])
    .set_index('BB')['id_B']
    .to_dict()
)

# Fill id_A by matching AA against BB
df['id_A'] = df['AA'].map(bb_to_idb)

# Save result
df.to_excel(output_file, index=False)

print(f"Done. Output saved to {output_file}")