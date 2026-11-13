"""Conservative section selection from explicit Markdown heading structure.

Unstructured text is kept intact: a keyword hit is not a section boundary.
This does not prove that an unstructured page describes only one policy.
"""
import re


def _title_key(value):
    return re.sub(r'[^\w가-힣]', '', value).casefold()


def select_policy_section(content, title):
    if not content or not title:
        return content
    title_lines = [line.strip() for line in title.splitlines() if line.strip()]
    target = _title_key(title_lines[-1])
    headings = list(re.finditer(r'^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$', content, re.MULTILINE))
    matches = [heading for heading in headings if _title_key(heading.group(2)) == target]
    # Repeated headings may be a table of contents or multiple local versions.
    # Do not silently pick the first one.
    if len(matches) != 1:
        return content
    start = matches[0]
    end = len(content)
    for heading in headings:
        if heading.start() > start.start() and len(heading.group(1)) <= len(start.group(1)):
            end = heading.start()
            break
    return content[start.start():end].strip()
