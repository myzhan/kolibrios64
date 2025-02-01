## FASM codestyle
### Rules
* No tab characters allowed.
* Code labels must be on a separate line without indent. It is not allowed to have a label and a command on the same line. Combinations of label and data in one line allowed.
* Lines with commands must start with 8 spaces. A mnemonic is short if its length is less than 8. Arguments for short mnemonics must start in the column 16. Arguments for long mnemonics must be separated from the mnemonic by exactly one space. Arguments must be separated with a comma and exactly one space after a comma. Arguments continued in the next line must start from the same position as in the first line.
* Prefixes `lock/rep[z|e|nz|ne]` are considered as a logical part of the command, so they start at position 8, then after exactly one space follows the main mnemonic and arguments.
* All code and text files should be in UTF-8 without BOM.

### Recommended
* Variables should have either a clear name or a comment.
* The same is advisable for labels, as well as put them in a separate line without indent.
* When you write a subroutine, it is very important to describe it's purpose, input and output data, spoiled registers like this: (TODO: mb use asmxygen)
```
; doing job
; in
; eax = some data
; ebx -> some pointer
; out
; eax = result data
; destroys ebx
subroutine:
```
* Only cast sizes when necessary, FASM keeps track of data types for you, use this feature to write flexible code.
* When you need to cast, do it on the memory location, not on the operand.

