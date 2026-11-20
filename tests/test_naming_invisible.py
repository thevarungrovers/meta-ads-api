"""Characters that render as nothing, or as a space that is not one, are
refused in a name: every Default_Ignorable_Code_Point (Hangul fillers, the
combining grapheme joiner, Mongolian and other variation selectors -- Lo or
Mn, not Cf), the blank Braille pattern U+2800, and every space separator
other than U+0020. A plain space and accented letters stay allowed."""

from __future__ import annotations

import pytest

from metaads_reporting.naming import Identity, NameRefused, invisible_characters, stamp

REFUSED = [0x115F, 0x1160, 0x3164, 0xFFA0, 0x034F, 0x180B, 0x180C, 0x180D, 0x180E, 0x180F,
           0xFE00, 0xFE0F, 0x2800, 0x00A0, 0x2007, 0x3000, 0x2002, 0x202F, 0x205F]


@pytest.mark.parametrize("code", REFUSED, ids=[f"U+{c:04X}" for c in REFUSED])
@pytest.mark.parametrize("identity", [Identity("AB", "max"), Identity()])
def test_an_ignorable_or_non_plain_space_character_is_refused(identity, code):
    name = f"spring{chr(code)}promo"
    assert invisible_characters(name) == [f"U+{code:04X}"]
    with pytest.raises(NameRefused, match=f"U\\+{code:04X}"):
        stamp(identity, name, entity_type="campaign")


@pytest.mark.parametrize("name", ["spring promo", "Café été", "Summer – Sale"])
def test_a_plain_space_and_accented_letters_are_allowed(name):
    assert invisible_characters(name) == []
    assert stamp(Identity("AB", "max"), name, entity_type="ad").final == f"AB-max-{name}"
