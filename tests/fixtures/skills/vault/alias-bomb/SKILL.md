---
a: &a ["x", "x", "x", "x", "x", "x", "x", "x", "x"]
b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]
c: [*b, *b, *b, *b, *b, *b, *b, *b, *b]
name: alias-bomb
description: YAML anchors/aliases are refused.
---
Body.
