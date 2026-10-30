# TODO

- [x] Add context compaction — implemented with append-only JSONL memory, start-of-turn and mid-turn checks, iterative structured summaries, and Pi-style recent-context cut points.
- [ ] Don't end a turn on an empty reply. A reply with no content and no tool calls currently counts as the final answer, so a provider hiccup or a model that reasons and then says nothing ends the run with an empty patch. Retry or nudge it ("your reply was empty, continue") a couple of times first. Seen on SWE-bench: MiMo v2.6 flash lost scikit-learn-25102 to it, and Qwen 3.8 flash via Alibaba ended all 10 hard instances on empty replies with no usage.
- [ ] Rerun Qwen 3.8 flash on the 10 hard Verified instances once empty replies are handled; its 0/10 (`qwen38flash-verified-hard-20261030-010237`) measures the empty replies, not the model.
