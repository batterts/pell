# Syntax grammar

This is an informal BNF-ish grammar for pell. Tokens are spelled in
double quotes; non-terminals are angle-bracketed; `[x]` is optional,
`{x}*` is zero-or-more, `x|y` is alternation.

For the canonical implementation, see `compiler/pell/parser.py`.

## Lexical structure

```
identifier   = letter { letter | digit | "_" }*
number       = digit { digit }* [ "." digit { digit }* ]
string       = '"' { char | escape | interp }* '"'
rawstring    = "`" { any-char }* "`"
regex        = "/" { any-char | "\\" any-char }* "/"
sql_block    = "sql!" "{" { any-char with brace tracking } "}"
jq_block    = "jq!" "{" { any-char with brace tracking } "}"

comment      = "//" { any-char until "\n" }
             | "/*" { any-char } "*/"

escape       = "\" ( "n" | "r" | "t" | "\"" | "\\" | "0" | other )
interp       = "{" expr "}"
```

`/regex/` is only recognized in regex-allowed token positions
(after operators, keywords, openers — see [Regex tutorial](../tutorial/11-regex.md)
for the disambiguation rule).

## Keywords

```
module import pub fn let return yield
if else for forall in match transaction
record error true false Some None Ok Err
unsafe finally and or not
type sealed aggregate case self
seq enum out inout
```

## Top-level

```
module       = "module" dotted_ident ";" { item }*

item         = annotations? "pub"? "unsafe"?
               ( fn_def | record_def | error_def | sealed_type_def
               | type_def | aggregate_def | seq_def | enum_def
               | import_def )

dotted_ident = ident { "." ident }*
qualified_ident = ident { "::" ident }*
```

## Functions

```
fn_def       = "fn" ident "(" param_list? ")" [ "->" type_ref ]
               block [ "finally" block ]

param_list   = param { "," param }*
param        = [ "out" | "inout" ] ident ":" type_ref

block        = "{" { stmt }* "}"
```

## Records and types

```
record_def       = "record" ident "{" field_list? "}"
field_list       = field { "," field }* [ "," ]
field            = ident ":" type_ref

error_def        = "error" ident "{" field_list? "}"

type_def         = "type" ident "{" { field_decl | method_def }* "}"
field_decl       = ident ":" type_ref ","
method_def       = "fn" ident "(" param_list? ")" [ "->" type_ref ] block

sealed_type_def  = "sealed" "type" ident "{" { case_decl | method_def }* "}"
case_decl        = "case" ident "{" field_list? "}"

aggregate_def    = "aggregate" ident "(" param_list ")" "->" type_ref
                   "{" "state" "{" field_list? "}"
                       "step" "(" param_list ")" block
                       [ "merge" "(" param_list ")" block ]
                       "finish" "(" ")" "->" type_ref block
                   "}"

seq_def          = "seq" qualified_ident ";"
enum_def         = "enum" ident "{" enum_variant { "," enum_variant }* [ "," ] "}"
enum_variant     = ident [ "=" string ]
```

## Type references

```
type_ref     = prim_type | named_type | optional_type | generic_type

prim_type    = "number" | "int" | "text" | "bigtext" | "bool"
             | "date" | "timestamp" | "interval" | "bytes" | "json"
             | "Unit" | "Never"

named_type   = ident | qualified_ident
optional_type = "Option" "<" type_ref ">"
generic_type  = ident "<" type_ref { "," type_ref }* ">"
```

Common generics: `list<T>`, `map<K, V>`, `set<T>`, `cursor<T>`,
`stream<T>`, `rowtype<table_name>`, `Result<T, E>`.

## Statements

```
stmt        = let_stmt | assign_stmt | expr_stmt | return_stmt
            | yield_stmt | if_stmt | for_stmt | forall_stmt
            | match_stmt | transaction_stmt

let_stmt    = "let" ident [ ":" type_ref ] [ "=" expr ] ";"
assign_stmt = ident "=" expr ";"
              | member_access "=" expr ";"
return_stmt = "return" [ expr ] ";"
yield_stmt  = "yield" expr ";"
expr_stmt   = expr ";"

if_stmt     = "if" expr block [ "else" ( if_stmt | block ) ]
for_stmt    = "for" ident "in" expr block
forall_stmt = "forall" ident "in" expr block
match_stmt  = "match" expr "{" match_arm { "," match_arm }* "}"
match_arm   = pattern "=>" ( expr | block )

transaction_stmt = "transaction" block
```

## Expressions

```
expr         = pipeline

pipeline     = logical_or { "|>" logical_or }*
logical_or   = logical_and { "||" logical_and }*
logical_and  = equality { "&&" equality }*
equality     = comparison { ( "==" | "!=" ) comparison }*
comparison   = addition { ( "<" | "<=" | ">" | ">=" ) addition }*
addition     = mult { ( "+" | "-" ) mult }*
mult         = unary { ( "*" | "/" | "%" ) unary }*
unary        = [ "!" | "-" ] postfix
postfix      = primary { "(" arg_list? ")"
                       | "." ident
                       | "::" ident
                       | "::" "<" type_ref { "," type_ref }* ">"
                       | "[" expr "]"
                       | "?"
                       }*

primary      = literal
             | ident
             | "(" expr ")"
             | block_expr
             | if_expr
             | match_expr
             | struct_lit
             | list_lit
             | "Ok" "(" expr ")"
             | "Err" "(" expr ")"
             | "Some" "(" expr ")"
             | "None"
             | sql_block
             | jq_block
             | regex
             | rawstring
             | string

literal      = number | string | rawstring | regex
             | "true" | "false"

arg_list     = arg { "," arg }*
arg          = expr | ident "=" expr   (keyword arg)

struct_lit   = ident "{" field_init_list? "}"
field_init   = ident [ ":" expr ]
list_lit     = "[" [ expr { "," expr }* ] "]"
```

## Patterns (match)

```
pattern      = wildcard_pattern
             | binding_pattern
             | variant_pattern
             | literal_pattern

wildcard_pattern = "_"
binding_pattern  = ident
variant_pattern  = ident [ "{" field_pat { "," field_pat }* [ "," ".." ] "}" ]
                          [ "(" pattern { "," pattern }* ")" ]
field_pat        = ident [ ":" pattern ]
```

## Annotations

```
annotation       = "@" ident [ "(" annotation_arg_list? ")" ]
annotation_arg_list = annotation_arg { "," annotation_arg }*
annotation_arg   = expr | ident "=" expr
```

## Method chains on `sql!{}`

```
sql_chain    = sql_block { lock_modifier }* terminator
lock_modifier = ".for_update" "(" ")"
              | ".nowait" "(" ")"
              | ".skip_locked" "(" ")"
              | ".wait" "(" expr ")"
              | ".for_update_of" "(" ident { "," ident }* ")"
terminator   = ".one" "(" ")"
             | ".one_or_none" "(" ")"
             | ".first" "(" ")"
             | ".collect" "(" ")"
             | ".rowcount" "(" ")"
             | ".returning" "::" "<" type_ref ">" "(" ")" "." (...)
```

`.if_empty(...)` and `.if_many(...)` chains can be added after `.one()`
to override the default NotFound / TooManyRows handlers.

## Reserved for future use

Pell reserves several keywords for features not yet in the surface:
`var`, `transaction` (transaction blocks are partially specced),
`open`, `close`, `for_update`. Don't use these as identifiers in new
code.
