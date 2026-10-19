import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";

// LLM-Wiki conventions on top of GFM:
//   [[page]] / [[page|label]]   -> wiki link (dimmed when the page doesn't exist)
//   [[source: file.md]]         -> source link
//   > [!warning] Title          -> callout box
//   SIGNOR-277418, PMID 123456  -> external links

function linkify(md: string, vault: string): string {
  // Leave fenced and inline code alone.
  return md
    .split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((chunk, i) => {
      if (i % 2 === 1) return chunk;
      return chunk
        .replace(/\[\[\s*source:\s*([^\]|]+?)\s*\]\]/gi, (_, f) =>
          `[${f}](/${vault}/sources/${encodeURIComponent(f)} "source")`)
        .replace(/\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g, (_, p, label) => {
          const name = p.trim().replace(/\.md$/, "");
          return `[${(label ?? name).trim()}](/${vault}/wiki/${encodeURIComponent(name)} "wiki")`;
        })
        .replace(/(?<![\[\/=\w-])(SIGNOR-[A-Z]*\d+)(?![\]\w])/g, (_, id) =>
          `[${id}](https://signor.uniroma2.it/relation_result.php?id=${id})`)
        .replace(/(?<![\[\/\w])PMIDs?:?\s*(\d{5,9})\b/g, (_, n) =>
          `[PMID ${n}](https://pubmed.ncbi.nlm.nih.gov/${n}/)`);
    })
    .join("");
}

/** remark plugin: `> [!type] Title` blockquotes become callouts. */
function remarkCallouts() {
  return (tree: any) => {
    const walk = (node: any) => {
      if (node.type === "blockquote") {
        const para = node.children?.[0];
        const text = para?.type === "paragraph" ? para.children?.[0] : null;
        const m = text?.type === "text" ? /^\[!(\w+)\][ \t]*([^\n]*)\n?/.exec(text.value) : null;
        if (m) {
          const kind = m[1].toLowerCase();
          text.value = text.value.slice(m[0].length);
          const title = m[2].trim() || kind[0].toUpperCase() + kind.slice(1);
          para.children.unshift({ type: "strong", children: [{ type: "text", value: title }] },
            { type: "break" });
          node.data = { hProperties: { className: ["callout", `callout-${kind}`] } };
        }
      }
      node.children?.forEach(walk);
    };
    walk(tree);
  };
}

export function Markdown({ text, vault, pages }: { text: string; vault: string; pages?: Set<string> }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkCallouts]}
        components={{
          a: ({ href = "", title, children }) => {
            if (href.startsWith("/")) {
              const isWiki = title === "wiki";
              const name = decodeURIComponent(href.split("/").pop() ?? "");
              const missing = isWiki && pages && !pages.has(name);
              return (
                <Link to={href} className={`${isWiki ? "wikilink" : "sourcelink"}${missing ? " missing" : ""}`}
                  title={missing ? "Page does not exist yet" : undefined}>
                  {children}
                </Link>
              );
            }
            return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
          },
        }}
      >
        {linkify(text, vault)}
      </ReactMarkdown>
    </div>
  );
}
