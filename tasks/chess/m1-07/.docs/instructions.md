# Mate in one

Black to move. There is a move that checkmates at once.

FEN: `5b2/p5k1/r1p5/2p1PP2/1PP3qN/3K2P1/N1Q1B1P1/4B3 b - - 0 38`

```
. . . . . b . .
p . . . . . k .
r . p . . . . .
. . p . P P . .
. P P . . . q N
. . . K . . P .
N . Q . B . P .
. . . . B . . .
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
