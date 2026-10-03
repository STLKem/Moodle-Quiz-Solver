import re

# Test the regex pattern on various input formats
patterns = [
    # Single-word options (should work)
    "(a) standing (b) stand (c) stance (d) status",
    # Multi-word options (problematic)
    "(a) nerve killing (b) nerve hurting (c) nerve racking (d) nerve splitting",
    "(a) speeded through (b) speeded along (c) speeded up (d) speeded in",
    # Multi-line format
    "(a) standing\n(b) stand\n(c) stance\n(d) status",
    "(a) nerve killing\n(b) nerve hurting\n(c) nerve racking\n(d) nerve splitting",
    # Mixed format
    "(a) nerve killing (b) nerve hurting\n(c) nerve racking (d) nerve splitting",
    # With apostrophe
    "(a) wouldn't (b) couldn't (c) shouldn't (d) mustn't",
]

# Original pattern
pat_old = re.compile(r'\b([a-d])\s*\)\s*([A-Za-z][A-Za-z\'/-]{0,25})\b')

# Fixed pattern: don't consume space before word
pat_new = re.compile(r'\b([a-d])\)\s*([A-Za-z][A-Za-z\'/-]{0,30}?)(?=\s*\([a-d]\)|$)')

# Alternative: capture up to next letter or end
pat_new2 = re.compile(r'\b([a-d])\)\s*([A-Za-z][A-Za-z\'/-]{0,30}?)(?=\s*\([a-d]\)|$)')

print("=== Testing patterns ===")
for text in patterns:
    print(f"Input: {repr(text[:80])}")
    old = pat_old.findall(text)
    new = pat_new.findall(text)
    new2 = pat_new2.findall(text)
    print(f"  Old: {old}")
    print(f"  New: {new}")
    print(f"  New2: {new2}")
    print()