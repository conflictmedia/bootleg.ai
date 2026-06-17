python3 aai.py --site canary \
        --system "respond with just the json in codeblock (wrapped in 3 backticks), make sure the filenames are test_data_*.json with * being 3 random numbers" \
        --prompt "generate random test data into 1 medium json files" \
        --agent-mode --chat-path "/" \
        --code-only \
        --write-files ./test/ \
        --stable-seconds 10 \
        --activity-timeout 6000
