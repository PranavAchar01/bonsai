# Mate in one

White to move. There is a move that checkmates at once.

FEN: `1r1k1br1/2p1pp1p/1Q3p2/1B1pn3/8/1bP1PP2/PP1K3P/1R4NR w - - 0 23`

```
. r . k . b r .
. . p . p p . p
. Q . . . p . .
. B . p n . . .
. . . . . . . .
. b P . P P . .
P P . K . . . P
. R . . . . N R
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
