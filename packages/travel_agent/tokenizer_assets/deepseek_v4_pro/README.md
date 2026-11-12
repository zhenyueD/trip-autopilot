# DeepSeek-V4-Pro tokenizer asset

`tokenizer.json` is an unmodified copy of the official DeepSeek-V4-Pro asset.

- Source repository: `deepseek-ai/DeepSeek-V4-Pro`
- Immutable revision: `b5968e9190ef611bbf34a7229255be88a0e937c1`
- Source URL: `https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/resolve/b5968e9190ef611bbf34a7229255be88a0e937c1/tokenizer.json`
- SHA-256: `8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf`

The message framing in `writer_tokenizer.py` is the bounded subset currently
used by writer requests from DeepSeek's official `encoding/encoding_dsv4.py` at
the same revision. That source file has SHA-256
`bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0`.
It covers `system,user` and the existing JSON-repair
`system,user,system` request only, in chat or thinking mode. Other shapes fail
closed before transport.

These are exact counts for the pinned **public reference encoding**. They are
not provider billing facts: the service can apply a private template or a
different deployed tokenizer. Provider-returned prompt/completion usage remains
authoritative and must be stored separately.

