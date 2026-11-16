# Changelog

## 0.2.0 - 2026-11-14

- A Read of a guarded file with a `limit` over `max_lines` is now denied, with the same file map as an unranged Read.
- A `limit` that is not an integer counts as missing, so the Read is treated as unranged.
- New config key `max_lines` (default 400): the largest `limit` allowed on a file over `threshold_kb`.
- A ranged Read whose requested lines add up to more than `threshold_kb` is denied, even when `limit` is within `max_lines`. The reason gives the range's size and the largest `limit` that fits from that `offset`, followed by the same file map as an unranged Read. When one line alone is over the threshold, it offers only bounded output (`grep -noE '.{0,80}TERM.{0,80}' FILE | head -n 20`, or `jq` for `.json` files) and says never to print the whole line.
