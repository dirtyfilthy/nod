# TODO

- [x] Add context compaction — implemented with append-only JSONL memory, start-of-turn and mid-turn checks, iterative structured summaries, and Pi-style recent-context cut points.
- [x] Don't end a turn on an empty reply — `Agent._complete` asks again up to `EMPTY_REPLY_RETRIES` (2) times when a reply has no text and no tool calls, dropping the empty replies but keeping their meta under `empty_replies`. Prompted by MiMo v2.6 flash losing scikit-learn-25102 and Qwen 3.8 flash via Alibaba ending all 10 hard instances on empty replies.
- [ ] Rerun Qwen 3.8 flash on the 10 hard Verified instances once empty replies are handled; its 0/10 (`qwen38flash-verified-hard-20261030-010237`) measures the empty replies, not the model.
