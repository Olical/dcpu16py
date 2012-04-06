#! /usr/bin/env python
"""
pyparsing based grammar for DCPU-16 0x10c assembler
"""

try:
    from itertools import izip_longest
except ImportError:
    from itertools import zip_longest as izip_longest

try:
    basestring
except NameError:
    basestring = str

import logging; log = logging.getLogger("dcpu16_asm")
log.setLevel(logging.DEBUG)

import argparse
import struct
import os

import pyparsing as P


# Run with "DEBUG=1 python ./asm_pyparsing.py"
DEBUG = "DEBUG" in os.environ

# otherwise \n is also treated as ignorable whitespace
P.ParserElement.setDefaultWhitespaceChars(" \t")

identifier = P.Word(P.alphas+"_", P.alphanums+"_")
label = P.Combine(P.Literal(":").suppress() + identifier)

comment = P.Literal(";").suppress() + P.restOfLine

register = (P.Or(P.CaselessKeyword(x) for x in "ABCIJXYZO") |
            P.CaselessKeyword("PC") |
            P.CaselessKeyword("SP")).addParseAction(P.upcaseTokens)

stack_op = P.CaselessKeyword("PEEK") | P.CaselessKeyword("POP") | P.CaselessKeyword("PUSH")

hex_literal = P.Combine(P.Literal("0x") + P.Word(P.hexnums))
dec_literal = P.Word(P.nums)

hex_literal.setParseAction(lambda s, l, t: int(t[0], 16))
dec_literal.setParseAction(lambda s, l, t: int(t[0]))

numeric_literal = hex_literal | dec_literal
literal = numeric_literal | identifier


instruction = P.oneOf("SET ADD SUB MUL DIV MOD SHL SHR AND BOR XOR IFE IFN IFG IFB JSR", caseless=True)
basic_operand = P.Group(register("register") | stack_op("stack_op") | literal("literal"))
indirect_expr = P.Group(literal("literal") + P.Literal("+") + register("register"))

register.addParseAction(P.upcaseTokens)
stack_op.addParseAction(P.upcaseTokens)
instruction.addParseAction(P.upcaseTokens)

indirection_content = (indirect_expr("expr") | basic_operand("basic"))
indirection = P.Group(
    (P.Literal("[").suppress() + indirection_content + P.Literal("]").suppress())
  | (P.Literal("(").suppress() + indirection_content + P.Literal(")").suppress())
     )
operand = basic_operand("basic") | indirection("indirect")

def make_words(data):
    return [a << 8 | b for a, b in izip_longest(data[::2], data[1::2],
                                                  fillvalue=0)]
def wordize_string(s, l, tokens):
    return make_words([ord(c) for c in tokens.string])

quoted_string = P.quotedString("string").addParseAction(P.removeQuotes).addParseAction(wordize_string)
datum = quoted_string | numeric_literal
def parse_data(string, loc, tokens):
    result = []
    for token in tokens:
        token = datum.parseString(token).asList()
        result.extend(token)
    return result

datalist = P.commaSeparatedList.copy().setParseAction(parse_data)
data = P.CaselessKeyword("DAT")("instruction") + P.Group(datalist)("data")

statement = P.Group(
    (instruction("instruction") +
     P.Group(operand)("first") +
     P.Optional(P.Literal(",").suppress() + P.Group(operand)("second"))
    ) | data
)

line = (P.Optional(label("label")) + 
        P.Optional(statement("statement"), default=None) +
        P.Optional(comment("comment")) + 
        P.lineEnd.suppress())

full_grammar = P.stringStart + P.OneOrMore(P.Group(line)) + P.stringEnd


if DEBUG:
    # Turn setdebug on for all parse elements
    for name, var in locals().copy().items():
        if isinstance(var, P.ParserElement):
            var.setName(name).setDebug()
    def debug_line(string, location, tokens):
        """
        Show the current line number and content being parsed
        """
        lineno = string[:location].count("\n")
        remaining = string[location:]
        line_end = remaining.index("\n") if "\n" in remaining else None
        log.debug("====")
        log.debug("  Parse line {0}".format(lineno))
        log.debug("  '{0}'".format(remaining[:line_end]))
        log.debug("====")
    line.setDebugActions(debug_line, None, None)

IDENTIFIERS = {"A": 0x0, "B": 0x1, "C": 0x2, "X": 0x3, "Y": 0x4, "Z": 0x5,
               "I": 0x6, "J": 0x7, "POP": 0x18, "PEEK": 0x19, "PUSH": 0x1A,
               "SP": 0x1B, "PC": 0x1C}
OPCODES = {"SET": 0x1, "ADD": 0x2, "SUB": 0x3, "MUL": 0x4, "DIV": 0x5,
           "MOD": 0x6, "SHL": 0x7, "SHR": 0x8, "AND": 0x9, "BOR": 0xA,
           "XOR": 0xB, "IFE": 0xC, "IFN": 0xD, "IFG": 0xE, "IFB": 0xF}
        
def process_operand(o):
    if o.basic:
        b = o.basic
        if b.register:
            return IDENTIFIERS[b.register], None
            
        elif b.stack_op:
            return IDENTIFIERS[b.stack_op], None
            
        elif b.literal is not None:
            l = b.literal
            if not isinstance(l, basestring) and l < 0x20:
                return 0x20 | l, None
            assert not l == "", o.asXML()
            return 0x1F, l
            
    elif o.indirect:
        i = o.indirect
        if i.basic:
            ib = i.basic
            if ib.register:
                assert ib.register in "ABCXYZIJ"
                return 0x8 + IDENTIFIERS[ib.register], None
                
            elif ib.literal is not None:
                return 0x1E, ib.literal
            
        elif i.expr:
            ie = i.expr
            assert ie.register in "ABCXYZIJ"
            return 0x10 | IDENTIFIERS[ie.register], ie.literal
    return None, None

def codegen(source, input_filename="<unknown>"):
    
    try:
        parsed = full_grammar.parseString(source)
    except P.ParseException as exc:
        log.fatal("Parse error:")
        log.fatal("  {0}:{1}:{2} HERE {3}"
                  .format(input_filename, exc.lineno, exc.col, exc.markInputline()))
        return None
    
    if DEBUG:
        from pprint import pprint
        print(parsed.asXML())
    
    labels = {}
    program = []
    
    for line in parsed:
        if line.label:
            labels[line.label] = len(program)
            
        s = line.statement
        if not s: continue
        
        if s.instruction == "DAT":
            program.extend(s.data)
            continue
        
        if s.instruction == "JSR":
            o = 0x00
            a = 0x01
            b, y = process_operand(s.first)
            
        else:
            o = OPCODES[s.instruction]
            a, x = process_operand(s.first)
            b, y = process_operand(s.second)
            
        program.append(((b << 10) + (a << 4) + o))
        if x is not None: program.append(x)
        if y is not None: program.append(y)
        
    # Substitute labels
    for i, c in enumerate(program):
        if isinstance(c, basestring):
            program[i] = labels[c]
    
    # Turn words into bytes
    result = bytes()
    for word in program:
        result += struct.pack(">H", word)
    return result

def main():
    parser = argparse.ArgumentParser(
        description='A simple pyparsing-based DCPU assembly compiler')
    parser.add_argument(
        'source', metavar='IN', type=str,
        help='file path of the file containing the assembly code')
    parser.add_argument(
        'destination', metavar='OUT', type=str, nargs='?',
        help='file path where to store the binary code')
    args = parser.parse_args()
    
    if not log.handlers:
        from sys import stderr
        handler = logging.StreamHandler(stderr)
        log.addHandler(handler)
        if not DEBUG: handler.setLevel(logging.INFO)
    
    with open(args.source) as fd:
        program = codegen(fd.read(), args.source)
    
    if program is None:
        log.fatal("No program produced.")
        if not DEBUG:
            log.fatal("Run with DEBUG=1 ./asm_pyparsing.py for more information.")
        return 1
    
    if not args.destination:
        print(program)
    else:
        with open(args.destination, "wb") as fd:
            fd.write(program)
        log.info("Program written to {0} ({1} bytes, hash={2})"
                 .format(args.destination, len(program), hex(abs(hash(program)))))
            
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
