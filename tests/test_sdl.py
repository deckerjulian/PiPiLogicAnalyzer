"""Signal Description Language tests."""

from __future__ import annotations

import numpy as np
import pytest

from pipilogicanalyzer.sdl import parser
from pipilogicanalyzer.sdl.parser import (
    DuplicatedGroupError,
    InvalidGroupError,
    InvalidTokenError,
    MissingGroupError,
    TokenizedSDL,
    samples_from_source,
)


def bits(samples: np.ndarray) -> str:
    return "".join(str(int(value)) for value in samples)


def test_levels_and_counts():
    assert bits(TokenizedSDL("$0;h3;l2;").to_samples()) == "11100"
    assert bits(TokenizedSDL("$1;=2;!3;").to_samples()) == "11000"


def test_hexadecimal_counts():
    assert TokenizedSDL("$0;h0x10;").to_samples().size == 16


def test_anonymous_group_repeats():
    assert bits(TokenizedSDL("$0;{h1,l1}3;").to_samples()) == "101010"


def test_trailing_comma_in_a_group_is_accepted():
    assert bits(TokenizedSDL("$0;{h1,l1,}3;").to_samples()) == "101010"


def test_named_groups_are_definitions_not_content():
    sdl = TokenizedSDL("$0;{<a>,h2,l1}1;[a];[a];")
    assert bits(sdl.to_samples()) == "110110"


def test_bytes_use_the_bit_groups():
    sdl = TokenizedSDL('$0;{<0>,l2}1;{<1>,h2}1;b0x05;')
    # 0x05 LSB first: 1 0 1 0 0 0 0 0, two samples per bit.
    assert bits(sdl.to_samples()) == "11" + "00" + "11" + "00" * 5


def test_msb_first_bytes():
    sdl = TokenizedSDL('$0;{<0>,l1}1;{<1>,h1}1;B0x05;')
    assert bits(sdl.to_samples()) == "00000101"


def test_strings_are_encoded_as_bytes():
    sdl = TokenizedSDL('$0;{<0>,l1}1;{<1>,h1}1;s"AB";')
    assert sdl.to_samples().size == 16


def test_comments_are_ignored():
    source = "$0; // a comment\nh2; /* block */ l2;"
    assert bits(TokenizedSDL(source).to_samples()) == "1100"


def test_missing_group_is_reported():
    with pytest.raises(MissingGroupError):
        TokenizedSDL("$0;[missing];")


def test_bytes_without_bit_groups_are_reported():
    with pytest.raises(MissingGroupError):
        TokenizedSDL("$0;b0x55;")


def test_duplicated_group_is_reported():
    with pytest.raises(DuplicatedGroupError):
        TokenizedSDL("{<a>,h1}1;{<a>,l1}1;")


def test_invalid_token_is_reported():
    with pytest.raises(InvalidTokenError):
        TokenizedSDL("$0;wat;")


def test_recursive_groups_do_not_overflow_the_stack():
    with pytest.raises(InvalidGroupError):
        TokenizedSDL("{<a>,[a]}1;[a];").to_samples()


def test_zero_repetition_is_rejected():
    with pytest.raises(InvalidGroupError):
        TokenizedSDL("{h1,l1}0;")


def test_samples_from_source_pads_with_the_last_level():
    assert bits(samples_from_source("$0;h2;", 5)) == "11111"
    assert bits(samples_from_source("$0;l2;", 5)) == "00000"


def test_samples_from_source_truncates():
    assert bits(samples_from_source("$0;h10;", 3)) == "111"


def test_token_types():
    assert parser.get_token_type("$1") is parser.TokenType.INITIAL_VALUE
    assert parser.get_token_type("h10") is parser.TokenType.VALUE
    assert parser.get_token_type("b0x10") is parser.TokenType.BYTE
    assert parser.get_token_type('s"x"') is parser.TokenType.STRING
    assert parser.get_token_type("[abc]") is parser.TokenType.GROUP_NAME
    assert parser.get_token_type("{h1,l1}2") is parser.TokenType.GROUP
    assert parser.get_token_type("???") is parser.TokenType.NONE
