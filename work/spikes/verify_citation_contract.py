import re


CITATION = re.compile(r"\[source:([a-z0-9-]+)#([a-z0-9-]+)\]")
known = {("requirements", "rto"), ("experiment", "canary")}
report = "Use canary [source:requirements#rto] [source:experiment#canary]."
citations = set(CITATION.findall(report))
assert citations <= known
assert len({source for source, _ in citations}) >= 2
