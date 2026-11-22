#!/bin/bash

# Define source and target directories
SOURCE_DIR="inputs_de"  # e.g., "2 - Bad Moon Rising"
TARGET_DIR="inputs_DE"  # e.g., "bmr"

# Loop through all .json files in the source directory
find "$SOURCE_DIR" -type f -name "*.json" | while read -r source_file; do
    # Extract the filename (without path)
    filename=$(basename "$source_file")

    # Find the corresponding file in the target directory
    target_file=$(find "$TARGET_DIR" -type f -name "$filename" | head -n 1)

    if [ -z "$target_file" ]; then
        echo "Warning: No matching file for '$filename' in target directory."
        continue
    fi

    # Extract the "ability" value from the source file
    ability_value=$(jq -r '.ability' "$source_file" 2>/dev/null)

    if [ -z "$ability_value" ] || [ "$ability_value" = "null" ]; then
        echo "Warning: No 'ability' attribute found in '$source_file'."
        continue
    fi

    # Update the "ability" attribute in the target file
    jq --arg val "$ability_value" '.ability = $val' "$target_file" > tmp.json && mv tmp.json "$target_file"

    echo "Updated '$target_file' with ability: '$ability_value'"
done

echo "Script completed."
