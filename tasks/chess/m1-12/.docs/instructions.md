# Mate in one

White to move. There is a move that checkmates at once.

FEN: `1rq1k2r/1pnb1p2/n1p2Q2/4p2p/NB1N4/1P1Pp1PP/P3BP1K/2R4R w - - 10 33`

```
. r q . k . . r
. p n b . p . .
n . p . . Q . .
. . . . p . . p
N B . N . . . .
. P . P p . P P
P . . . B P . K
. . R . . . . R
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
