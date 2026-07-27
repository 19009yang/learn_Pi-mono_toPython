---
name: hello-file
description: Create a hello.txt file containing hello, then read it back to verify the result.
disable-model-invocation: false
---

# Hello file

Use this skill when the user asks for a minimal file creation and verification example.

1. Use the `write` tool to create `hello.txt` with exactly `hello` as its content.
2. Use the `read` tool to read `hello.txt` after writing it.
3. Report the verified content and the relative file path.

Do not overwrite an existing `hello.txt`; tell the user if one already exists.
