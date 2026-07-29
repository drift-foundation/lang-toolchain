import sys; sys.path.insert(0,'/home/sl/src/drift-lang'); sys.path.insert(0,'/home/sl/src/drift-lang/lang')
from lang.driftc.parser.parser import parse_program
src='''
module m;
struct Foo { a: Int }
variant Opt<T> { Some(v: T), None }
fn g(x: &Int) -> Void {}
fn h() -> Void {
  var s = 1;
  val f = Foo(a = 2);
  val o = Opt<&Int>::Some(&s);
  g(&s);
  g(&"lit");
  g(&mk());
  arr.push(&s);
  cb.call(&s);
  val z = (|p: &Int| => 1) (&s);
  println!("x", &s);
}
'''
p=parse_program(src, filename='p')
import dataclasses
def show(n,d=0):
    print(' '*d+type(n).__name__, getattr(n,'op','') , getattr(n,'ident','') or getattr(n,'attr','') or getattr(n,'member',''))
    if dataclasses.is_dataclass(n):
        for f in dataclasses.fields(n):
            v=getattr(n,f.name)
            if isinstance(v,(list,tuple)):
                for x in v:
                    if dataclasses.is_dataclass(x) and not type(x).__name__ in ('Located','TypeExpr'): show(x,d+2)
            elif dataclasses.is_dataclass(v) and not type(v).__name__ in ('Located','TypeExpr'): show(v,d+2)
for fn in p.functions:
    if fn.name=='h':
        for st in fn.body.statements: show(st)
