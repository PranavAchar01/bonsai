# Mate in one

White to move. There is a move that checkmates at once.

FEN: `1rbr3k/p2p3p/p1p1p3/4PNQ1/P1PP4/2b1B3/7P/3K1BR1 w - - 9 34`

```
. r b r . . . k
p . . p . . . p
p . p . p . . .
. . . . P N Q .
P . P P . . . .
. . b . B . . .
. . . . . . . P
. . . K . B R .
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
