# Mate in one

White to move. There is a move that checkmates at once.

FEN: `1nk3nr/Q3p2p/1p5P/8/2P3P1/P2pNR1P/1P1P4/1RBK3B w - - 3 36`

```
. n k . . . n r
Q . . . p . . p
. p . . . . . P
. . . . . . . .
. . P . . . P .
P . . p N R . P
. P . P . . . .
. R B K . . . B
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
