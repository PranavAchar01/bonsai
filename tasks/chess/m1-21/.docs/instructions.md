# Mate in one

White to move. There is a move that checkmates at once.

FEN: `1R6/4k3/4rp1p/1B1pPQB1/3p2p1/5RPP/1p5N/RN3K1b w - - 1 43`

```
. R . . . . . .
. . . . k . . .
. . . . r p . p
. B . p P Q B .
. . . p . . p .
. . . . . R P P
. p . . . . . N
R N . . . K . b
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
