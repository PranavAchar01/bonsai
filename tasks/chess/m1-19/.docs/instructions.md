# Mate in one

White to move. There is a move that checkmates at once.

FEN: `k1r5/5R2/7n/1ppr1Q1p/7P/1p1np1P1/7p/KN5q w - - 5 45`

```
k . r . . . . .
. . . . . R . .
. . . . . . . n
. p p r . Q . p
. . . . . . . P
. p . n p . P .
. . . . . . . p
K N . . . . . q
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
