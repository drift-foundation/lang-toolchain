import gdb


def _lookup_type(type_name: str):
	try:
		return gdb.lookup_type(type_name)
	except gdb.error:
		return None


def _as_string_type():
	ty = _lookup_type("String")
	if ty is not None:
		return ty
	ty = _lookup_type("struct String")
	if ty is not None:
		return ty
	return None


def _eval_expr(expr: str):
	return gdb.parse_and_eval(expr)


def _deref_if_ptr(val):
	try:
		if val.type.code == gdb.TYPE_CODE_PTR:
			return val.dereference()
	except Exception:
		return val
	return val


def _get_field(val, name: str):
	try:
		return val[name]
	except Exception:
		return None


def _read_bytes(addr: int, length: int) -> bytes:
	mem = gdb.selected_inferior().read_memory(addr, length)
	return mem.tobytes()


def _print_bytes_as_utf8(data: bytes):
	try:
		text = data.decode("utf-8", "replace")
	except Exception:
		text = data.decode("utf-8", "replace")
	print(text)


class PString(gdb.Command):
	"""p_string <expr> -- Print a Drift String (len+data) as UTF-8."""
	def __init__(self):
		super().__init__("p_string", gdb.COMMAND_DATA)

	def invoke(self, arg, from_tty):
		arg = arg.strip()
		if not arg:
			print("usage: p_string <expr>")
			return
		val = _eval_expr(arg)
		val = _deref_if_ptr(val)
		len_val = _get_field(val, "len")
		data_val = _get_field(val, "data")
		if len_val is None or data_val is None:
			print("p_string: value does not look like a String {len, data}")
			return
		try:
			length = int(len_val)
			addr = int(data_val)
		except Exception:
			print("p_string: failed to read len/data")
			return
		if length <= 0:
			print("")
			return
		data = _read_bytes(addr, length)
		_print_bytes_as_utf8(data)


class PArray(gdb.Command):
	"""p_array <expr> <count> <elem_type> -- Print elements from Array header."""
	def __init__(self):
		super().__init__("p_array", gdb.COMMAND_DATA)

	def invoke(self, arg, from_tty):
		args = arg.strip().split()
		if len(args) < 3:
			print("usage: p_array <expr> <count> <elem_type>")
			return
		expr = args[0]
		try:
			count = int(args[1], 0)
		except Exception:
			print("p_array: invalid count")
			return
		elem_type = " ".join(args[2:])
		val = _eval_expr(expr)
		val = _deref_if_ptr(val)
		data_val = _get_field(val, "data")
		if data_val is None:
			print("p_array: value does not look like an Array {data}")
			return
		elem_ty = _lookup_type(elem_type)
		if elem_ty is None:
			elem_ty = _lookup_type(f"struct {elem_type}")
		if elem_ty is None:
			print(f"p_array: unknown element type '{elem_type}'")
			return
		ptr_ty = elem_ty.pointer()
		try:
			base = data_val.cast(ptr_ty)
		except Exception:
			try:
				base = gdb.Value(int(data_val)).cast(ptr_ty)
			except Exception:
				print("p_array: failed to cast data pointer")
				return
		for i in range(count):
			try:
				elt = (base + i).dereference()
				print(f"[{i}] = {elt}")
			except Exception as exc:
				print(f"[{i}] = <error: {exc}>")
				break


class PArrayStr(gdb.Command):
	"""p_array_str <expr> <count> -- Print Array<String> contents as text."""
	def __init__(self):
		super().__init__("p_array_str", gdb.COMMAND_DATA)

	def invoke(self, arg, from_tty):
		args = arg.strip().split()
		if len(args) < 2:
			print("usage: p_array_str <expr> <count>")
			return
		expr = args[0]
		try:
			count = int(args[1], 0)
		except Exception:
			print("p_array_str: invalid count")
			return
		val = _eval_expr(expr)
		val = _deref_if_ptr(val)
		data_val = _get_field(val, "data")
		if data_val is None:
			print("p_array_str: value does not look like an Array {data}")
			return
		str_ty = _as_string_type()
		if str_ty is None:
			print("p_array_str: String type not found")
			return
		ptr_ty = str_ty.pointer()
		try:
			base = data_val.cast(ptr_ty)
		except Exception:
			try:
				base = gdb.Value(int(data_val)).cast(ptr_ty)
			except Exception:
				print("p_array_str: failed to cast data pointer")
				return
		for i in range(count):
			try:
				elt = (base + i).dereference()
				len_val = _get_field(elt, "len")
				data_ptr = _get_field(elt, "data")
				if len_val is None or data_ptr is None:
					print(f"[{i}] = <not a String>")
					continue
				length = int(len_val)
				addr = int(data_ptr)
				data = _read_bytes(addr, length) if length > 0 else b""
				text = data.decode("utf-8", "replace")
				print(f"[{i}] = \"{text}\"")
			except Exception as exc:
				print(f"[{i}] = <error: {exc}>")
				break


class PArrayInt(gdb.Command):
	"""p_array_int <expr> <count> -- Print Array<Int> contents."""
	def __init__(self):
		super().__init__("p_array_int", gdb.COMMAND_DATA)

	def invoke(self, arg, from_tty):
		args = arg.strip().split()
		if len(args) < 2:
			print("usage: p_array_int <expr> <count>")
			return
		expr = args[0]
		count = args[1]
		PArray().invoke(f"{expr} {count} Int", from_tty)


class PArrayByte(gdb.Command):
	"""p_array_byte <expr> <count> -- Print Array<Byte> contents."""
	def __init__(self):
		super().__init__("p_array_byte", gdb.COMMAND_DATA)

	def invoke(self, arg, from_tty):
		args = arg.strip().split()
		if len(args) < 2:
			print("usage: p_array_byte <expr> <count>")
			return
		expr = args[0]
		count = args[1]
		PArray().invoke(f"{expr} {count} Byte", from_tty)


class PArrayFloat(gdb.Command):
	"""p_array_float <expr> <count> -- Print Array<Float> contents."""
	def __init__(self):
		super().__init__("p_array_float", gdb.COMMAND_DATA)

	def invoke(self, arg, from_tty):
		args = arg.strip().split()
		if len(args) < 2:
			print("usage: p_array_float <expr> <count>")
			return
		expr = args[0]
		count = args[1]
		PArray().invoke(f"{expr} {count} Float", from_tty)


PString()
PArray()
PArrayStr()
PArrayInt()
PArrayByte()
PArrayFloat()
