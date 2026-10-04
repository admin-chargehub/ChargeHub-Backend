# Vendored code

Files copied verbatim from other repositories. **Do not edit them here.**

| File | Source | Pinned at |
|---|---|---|
| `chargehub_codes.py` | [ChargeHub-Contracts](https://github.com/admin-chargehub/ChargeHub-Contracts) `reference/python/chargehub_codes.py` | `4e091c3` |

`chargehub_codes.py` is byte-identical to the contracts copy, and that matters:
CI in the contracts repo holds it and the C implementation that firmware uses to
the same test vectors. A local edit here would silently desynchronise the
platform from the stations — which is the exact failure the contracts repo
exists to prevent.

To update: re-copy from the contracts repo, bump the commit above, and run
`tests/test_access_code_vectors.py`, which re-checks this copy against
`vectors/access-code.vectors.json`.
