// Minimal markdown for the shape spec. Escape-first: no raw HTML from the file
// ever reaches the page. Covers what SPEC.md actually uses — headings, prose,
// lists, fenced code, tables, quotes, rules, and the usual inline marks.
(function () {
  function esc(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function inline(text) {
    const code = [];
    let out = String(text).replace(/`([^`]+)`/g, (_, span) => {
      code.push(span);
      return `\u0000${code.length - 1}\u0000`;
    });
    out = esc(out);
    out = out.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+|#[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>'
    );
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\u0000(\d+)\u0000/g, (_, index) => {
      return `<code>${esc(code[Number(index)])}</code>`;
    });
    return out;
  }

  function splitRow(line) {
    return line
      .replace(/^\s*\|/, "")
      .replace(/\|\s*$/, "")
      .split("|")
      .map((cell) => cell.trim());
  }

  function render(markdown) {
    const lines = String(markdown).replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let index = 0;
    const isBlockStart = (line) =>
      /^```/.test(line) ||
      /^#{1,6}\s+/.test(line) ||
      /^\s*([-*+]|\d+\.)\s+/.test(line) ||
      /^>\s?/.test(line) ||
      /^\|/.test(line) ||
      /^(-{3,}|\*{3,}|_{3,})\s*$/.test(line);

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(/^```(\w*)/);
      if (fence) {
        index += 1;
        const body = [];
        while (index < lines.length && !/^```/.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1;
        const label = fence[1] ? ` data-lang="${esc(fence[1])}"` : "";
        out.push(
          `<pre class="md-code"${label}><code>${esc(body.join("\n"))}</code></pre>`
        );
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = Math.min(6, heading[1].length + 1);
        out.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
        index += 1;
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        out.push("<hr>");
        index += 1;
        continue;
      }

      if (/^\|/.test(line) && /^\|[\s:|-]+\|?\s*$/.test(lines[index + 1] || "")) {
        const head = splitRow(line);
        index += 2;
        const rows = [];
        while (index < lines.length && /^\s*\|/.test(lines[index])) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        const cells = (row, tag) =>
          row.map((cell) => `<${tag}>${inline(cell)}</${tag}>`).join("");
        out.push(
          `<div class="md-table-wrap"><table><thead><tr>${cells(head, "th")}</tr></thead>` +
            `<tbody>${rows
              .map((row) => `<tr>${cells(row, "td")}</tr>`)
              .join("")}</tbody></table></div>`
        );
        continue;
      }

      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (index < lines.length) {
          const item = lines[index].match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
          if (!item) break;
          items.push(item[2]);
          index += 1;
          // Continuation lines are indented prose belonging to the same item.
          while (
            index < lines.length &&
            /^\s{2,}\S/.test(lines[index]) &&
            !/^\s*([-*+]|\d+\.)\s+/.test(lines[index]) &&
            !isBlockStart(lines[index].trim())
          ) {
            items[items.length - 1] += ` ${lines[index].trim()}`;
            index += 1;
          }
        }
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>${items.map((item) => `<li>${inline(item)}</li>`).join("")}</${tag}>`);
        continue;
      }

      if (/^>\s?/.test(line)) {
        const quote = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) {
          quote.push(lines[index].replace(/^>\s?/, ""));
          index += 1;
        }
        out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
        continue;
      }

      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      out.push(`<p>${inline(paragraph.join(" "))}</p>`);
    }

    return out.join("\n");
  }

  window.renderMarkdown = render;
})();
