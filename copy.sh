#!/bin/bash

# Define the directories
INPUTS_DE="inputs_de"
INPUTS_NEW="inputs_new"

# Check if the directories exist
if [ ! -d "$INPUTS_DE" ] || [ ! -d "$INPUTS_NEW" ]; then
    echo "Error: One or both directories do not exist."
    exit 1
fi

# Use find to locate all JSON files in inputs_de and its subdirectories
find "$INPUTS_DE" -type f -name "*.json" | while read -r file_de; do
    # Extract the basename of the file
    filename=$(basename "$file_de")

    # Find the corresponding file in inputs_new by basename
    file_new=$(find "$INPUTS_NEW" -type f -name "$filename" | head -n 1)

    # Check if the corresponding file exists in inputs_new
    if [ -n "$file_new" ]; then
        # Extract the "ability" value from the file in inputs_de
        ability_value=$(jq -r '.ability' "$file_de")

        # Check if the extraction was successful
        if [ "$ability_value" != "null" ]; then
            # Replace the "ability" value in the file in inputs_new
            jq --arg new_ability "$ability_value" '.ability = $new_ability' "$file_new" > tmp.json && mv tmp.json "$file_new"
            echo "Updated 'ability' in $filename"
        else
            echo "Warning: 'ability' not found in $filename (inputs_de)"
        fi
    else
        echo "Warning: $filename not found in $INPUTS_NEW"
    fi
done

echo "Script completed."

