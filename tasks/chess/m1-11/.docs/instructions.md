# Mate in one

White to move. There is a move that checkmates at once.

FEN: `1n2k1r1/r1ppn2p/P3Q1p1/p1B1P3/2N5/bPP2p2/2K1PPPq/R4BNR w - - 4 25`

```
. n . . k . r .
r . p p n . . p
P . . . Q . p .
p . B . P . . .
. . N . . . . .
b P P . . p . .
. . K . P P P q
R . . . . B N R
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
