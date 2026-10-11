# Mate in one

Black to move. There is a move that checkmates at once.

FEN: `1r3bk1/6p1/r2P4/P1P5/P7/n4nB1/5P1P/2K1R1NR b - - 3 44`

```
. r . . . b k .
. . . . . . p .
r . . P . . . .
P . P . . . . .
P . . . . . . .
n . . . . n B .
. . . . . P . P
. . K . R . N R
```

Uppercase letters are White, lowercase are Black, rank 8 is the top row and file a is the left column.

Write the move to `answer.txt` in UCI form (from-square then to-square, for example `e2e4`, or `e7e8q` for a promotion). One move, nothing else.
