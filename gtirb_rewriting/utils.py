# GTIRB-Rewriting Rewriting API for GTIRB
# Copyright (C) 2021 GrammaTech, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# This project is sponsored by the Office of Naval Research, One Liberty
# Center, 875 N. Randolph Street, Arlington, VA 22203 under contract #
# N68335-17-C-0700.  The content of the information does not necessarily
# reflect the position or policy of the Government and no official
# endorsement should be inferred.
import logging
from typing import (
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    Sequence,
    TypeVar,
    Union,
    overload,
)

import capstone
import gtirb
from gtirb_capstone.instructions import GtirbInstructionDecoder

T = TypeVar("T")


class OffsetMapping(MutableMapping[gtirb.Offset, T]):
    """Mapping that allows looking up groups of items by their offset element.

    The keys in this mapping are required to be Offsets. If a non-Offset is
    used as a key, it is assumed to be the element_id of an Offset. In that
    case, the corresponding element is a MutableMapping[int, T] of
    displacements to values for every Offset that has the given element_id. For
    example,
        m = OffsetMapping[str]()
        m[Offset(x, 0)] = "a"     # insert an offset into the map
        m[x] = {1: "b", 2: "c"}   # insert two offsets into the map
        m[x][0] = "d"             # change the value for Offset(x, 0)
        print(m[Offset(x, 1)])    # get the value for Offset(x, 1)
        del m[Offset(x, 2)]       # delete Offset(x, 2) from the map
    """

    def __init__(self, *args, **kw):
        """Create a new OffsetMapping from an iterable and/or keywords."""
        self._data = {}
        self.update(*args, **kw)

    def __len__(self) -> int:
        """Get the number of Offsets stored in this mapping."""
        return sum(len(subdata) for subdata in self._data.values())

    def __iter__(self) -> Iterator[gtirb.Offset]:
        """"Yield the Offsets in this mapping."""
        for elem, subdata in self._data.items():
            for disp in subdata:
                yield gtirb.Offset(elem, disp)

    @overload
    def __getitem__(self, key: gtirb.Offset) -> T:
        ...

    @overload
    def __getitem__(self, key: gtirb.Node) -> MutableMapping[int, T]:
        ...

    def __getitem__(self, key):
        """Get the value for an Offset or dictionary for an element_id."""
        if isinstance(key, gtirb.Offset):
            elem, disp = key
            if elem in self._data and disp in self._data[elem]:
                return self._data[elem][disp]
        return self._data[key]

    @overload
    def __setitem__(self, key: gtirb.Offset, value: T) -> None:
        ...

    @overload
    def __setitem__(self, key: gtirb.Node, value: Mapping[int, T]) -> None:
        ...

    def __setitem__(self, key, value):
        """Set the value for an Offset, or several Offsets given an element."""
        if isinstance(key, gtirb.Offset):
            elem, disp = key
            if elem not in self._data:
                self._data[elem] = {}
            self._data[elem][disp] = value
        elif not isinstance(value, Mapping):
            raise ValueError("not a mapping: %r" % value)
        else:
            self._data.setdefault(key, {}).update(value)

    def __delitem__(self, key: Union[gtirb.Offset, gtirb.Node]) -> None:
        """Delete the mapping for an Offset or all Offsets given an element."""
        if isinstance(key, gtirb.Offset):
            elem, disp = key
            if elem not in self._data or disp not in self._data[elem]:
                raise KeyError(key)
            del self._data[elem][disp]
        else:
            del self._data[key]


def _target_triple(module: gtirb.Module) -> str:
    """
    Generate the appropriate LLVM target triple for a GTIRB Module.
    """

    if module.isa == gtirb.Module.ISA.X64:
        arch = "x86_64"
    elif module.isa == gtirb.Module.ISA.IA32:
        arch = "i386"
    elif module.isa == gtirb.Module.ISA.ARM:
        arch = "arm"
    elif module.isa == gtirb.Module.ISA.ARM64:
        arch = "arm64"
    else:
        assert False, f"Unsupported ISA: {module.isa}"

    if module.file_format == gtirb.Module.FileFormat.ELF:
        vendor = "pc"
        os = "linux"
    elif module.file_format == gtirb.Module.FileFormat.PE:
        vendor = "pc"
        os = "win32"
    else:
        assert False, f"Unsupported file format: {module.file_format}"

    return f"{arch}-{vendor}-{os}"


def _nonterminator_instructions(
    block: gtirb.CodeBlock, disassembly: Sequence[capstone.CsInsn]
) -> Iterator[capstone.CsInsn]:
    """
    Yields all instructions in a block of diassembly except for the terminator,
    if present.
    """
    if all(
        edge.label.type == gtirb.Edge.Type.Fallthrough
        for edge in block.outgoing_edges
    ):
        yield from disassembly
    else:
        yield from disassembly[:-1]


def show_block_asm(
    block: gtirb.CodeBlock,
    arch: gtirb.Module.ISA = None,
    logger=logging.getLogger(),
    decoder=None,
) -> None:
    """
    Disassemble and print the contents of a code block using the given
    architecture. If no architecture is given, it is taken from the block's
    module. If the block is not in a module, the function throws an error.
    """

    if decoder is None:
        if arch is None:
            if block.module is None:
                raise ValueError("Undefined architecture")
            arch = block.byte_interval.section.module.isa
        decoder = GtirbInstructionDecoder(arch)

    if block.contents:
        for i in decoder.get_instructions(block):
            logger.debug("\t0x%x:\t%s\t%s", i.address, i.mnemonic, i.op_str)


def _substitute_block(
    old_block: gtirb.CodeBlock,
    new_block: gtirb.CodeBlock,
    cfg: gtirb.CFG,
    symbols: Iterable[gtirb.Symbol],
) -> None:
    """
    Substitutes one block for another by adjusting the CFG and any symbols
    pointing at the block.
    """

    for edge in set(cfg.in_edges(old_block)):
        cfg.discard(edge)
        cfg.add(edge._replace(target=new_block))

    for edge in set(cfg.out_edges(old_block)):
        cfg.discard(edge)
        cfg.add(edge._replace(source=new_block))

    for sym in symbols:
        if sym.referent == old_block:
            sym.referent = new_block


def _modify_block_insert(
    block: gtirb.CodeBlock,
    offset: int,
    new_bytes: bytes,
    new_blocks: List[gtirb.CodeBlock],
    new_cfg: gtirb.CFG,
    new_symbolic_expressions: Dict[int, gtirb.SymbolicExpression],
    new_symbols: Iterable[gtirb.Symbol],
) -> None:
    """
    Insert bytes into a block and adjusts the IR as needed.
    """

    n_bytes = len(new_bytes)
    bi = block.byte_interval
    assert bi

    # Ideally none of this would be necessary, but there is non-determinism in
    # gtirb-pprinter that causes it to issue warnings with zero-sized blocks,
    # so we'll avoid creating one here.
    if offset == 0:
        new_blocks[-1].size += block.size
        block.size = new_blocks[0].size
        _substitute_block(new_blocks[0], block, new_cfg, new_symbols)
        del new_blocks[0]
    else:
        new_blocks[-1].size += block.size - offset
        block.size = offset

        # Add an edge from the original block to the first patch block.
        new_cfg.add(
            gtirb.Edge(
                source=block,
                target=new_blocks[0],
                label=gtirb.Edge.Label(type=gtirb.Edge.Type.Fallthrough),
            )
        )

    if new_blocks:
        # Alter any outgoing edges from the original block originate from the
        # last patch block.
        for edge in set(block.outgoing_edges):
            bi.module.ir.cfg.discard(edge)
            bi.module.ir.cfg.add(edge._replace(source=new_blocks[-1]))

    offset += block.offset

    # adjust byte interval the block goes in
    bi.size += n_bytes
    bi.contents = bi.contents[:offset] + new_bytes + bi.contents[offset:]

    # adjust blocks that occur after the insertion point
    # TODO: what if blocks overlap over the insertion point?
    for b in bi.blocks:
        if b != block and b.offset >= offset:
            b.offset += n_bytes

    # adjust all of the new blocks to be relative to the byte interval and
    # add them to the byte interval
    for b in new_blocks:
        b.offset += offset

    bi.blocks.update(new_blocks)

    # adjust sym exprs that occur after the insertion point
    bi.symbolic_expressions = {
        (k + n_bytes if k >= offset else k): v
        for k, v in bi.symbolic_expressions.items()
    }

    # add all of the symbolic expressions from the code we're inserting
    for rel_offset, expr in new_symbolic_expressions.items():
        bi.symbolic_expressions[offset + rel_offset] = expr

    bi.module.ir.cfg.update(new_cfg)
    bi.module.symbols.update(new_symbols)

    # adjust aux data if present
    def update_aux_data_keyed_by_offset(name):
        table = bi.module.aux_data.get(name)
        if table:
            if not isinstance(table.data, OffsetMapping):
                table.data = OffsetMapping(table.data)
            if bi in table.data:
                displacement_map = table.data[bi]
                del table.data[bi]
                table.data[bi] = {
                    (k + n_bytes if k >= offset else k): v
                    for k, v in displacement_map.items()
                }

    # TODO: It seems like we _could_ detect any aux data table that is a
    #       mapping using Offset as keys if gtirb.Serialization._parse_type
    #       were public.
    update_aux_data_keyed_by_offset("comments")
    update_aux_data_keyed_by_offset("padding")
    update_aux_data_keyed_by_offset("symbolicExpressionSizes")


def _is_elf_pie(module: gtirb.Module) -> bool:
    return (
        module.file_format == gtirb.Module.FileFormat.ELF
        and "DYN" in module.aux_data["binaryType"].data
    )


def decorate_extern_symbol(module: gtirb.Module, sym: str) -> str:
    """
    Decorates a symbol as needed for the target. For example, this might
    involve adding '@PLT' for position independent ELF executables.
    """
    # TODO: 32-bit Windows uses a leading underscore. So does Mach-O.
    if _is_elf_pie(module):
        return sym + "@PLT"
    return sym


def effective_alignment(address: int, max_alignment: int = 8) -> int:
    """Return the largest power of two to which an address is aligned."""
    return (~address & (address - 1) & (max_alignment - 1)) + 1


def align_address(address: int, alignment: int) -> int:
    """Increase an address to the next alignment boundary, if necessary."""
    return (address + alignment - 1) & -alignment
