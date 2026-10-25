import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown, parseBlocks, parseInline } from "./markdown";

const html = (s: string) => renderToStaticMarkup(<Markdown text={s} />);

describe("markdown reader", () => {
  it("makes paragraphs with bold, italic and code", () => {
    expect(html("You spent **₹250** on *food* via `swiggy`.")).toBe(
      "<div><p>You spent <strong>₹250</strong> on <em>food</em> via <code>swiggy</code>.</p></div>",
    );
  });

  it("never turns text into markup", () => {
    const out = html('<script>alert(1)</script> <img src=x onerror=alert(1)> **b**');
    expect(out).not.toContain("<script");
    expect(out).not.toContain("<img");
    expect(out).toContain("&lt;script&gt;");
    expect(out).toContain("<strong>b</strong>");
  });

  it("links only to http and https addresses", () => {
    expect(html("[ok](https://example.com/a?b=1)")).toContain('<a href="https://example.com/a?b=1" target="_blank" rel="noopener noreferrer">ok</a>');
    expect(html("[bad](javascript:alert(1))")).not.toContain("<a ");
    expect(html("[bad](data:text/html;base64,AAAA)")).not.toContain("<a ");
    expect(html("[bad](//evil.example/x)")).not.toContain("<a ");
  });

  it("reads bullet and numbered lists", () => {
    expect(html("- one\n- two")).toBe("<div><ul><li>one</li><li>two</li></ul></div>");
    expect(html("1. first\n2. second")).toBe("<div><ol><li>first</li><li>second</li></ol></div>");
  });

  it("reads headings at a calm size, never an h1 or h2", () => {
    const blocks = parseBlocks("# Big\n## Medium\n#### Small");
    expect(blocks.map((b) => (b.t === "h" ? b.level : 0))).toEqual([3, 4, 5]);
  });

  it("reads a table", () => {
    const out = html("| Category | Amount |\n|---|---|\n| Food | ₹3,000 |\n| Travel | ₹500 |");
    expect(out).toContain("<th scope=\"col\">Category</th>");
    expect(out).toContain("<td>₹3,000</td>");
    expect(out.match(/<tr>/g)?.length).toBe(3);
  });

  it("keeps a code fence as plain text", () => {
    expect(html("```\n<b>x</b>\n```")).toBe("<div><pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre></div>");
  });

  it("keeps single newlines as line breaks and blank lines as paragraphs", () => {
    expect(html("a\nb\n\nc")).toBe("<div><p>a<br/>b</p><p>c</p></div>");
  });

  it("leaves unmatched marks alone", () => {
    expect(parseInline("2 * 3 = 6 and a_b")).toEqual([{ t: "text", v: "2 * 3 = 6 and a_b" }]);
  });

  it("copes with empty input", () => {
    expect(html("")).toBe("<div></div>");
  });
});
