import pytest
from lark.exceptions import UnexpectedInput

from lang.driftc.parser import parser as p


def test_type_keyword_is_reserved() -> None:
	with pytest.raises(UnexpectedInput):
		p.parse_program(
			"""
fn main() -> Int {
	val type: Int = 1;
	return type;
}
"""
		)
