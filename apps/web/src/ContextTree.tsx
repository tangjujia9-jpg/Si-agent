import { useState } from "react";
import { ChevronRight, FileText, Folder } from "lucide-react";
import type { ProjectNode } from "./api";
import { nodeTitle } from "./ui";

export function ContextTree({
  nodes,
  inspect,
}: {
  nodes: ProjectNode[];
  inspect: (node: ProjectNode) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const uris = new Set(nodes.map((n) => n.uri));
  function branch(rows: ProjectNode[], depth: number): React.ReactNode {
    return rows.map((n) => {
      const children = nodes
        .filter((c) => c.parent_uri === n.uri)
        .sort((a, b) => a.uri.localeCompare(b.uri));
      const isOpen = depth === 0 ? !expanded.has(n.id) : expanded.has(n.id);
      return (
        <div key={n.id}>
          <div className="context-tree-row" style={{ paddingLeft: depth * 12 }}>
            {children.length > 0 ? (
              <button
                className="icon-button"
                aria-label={`${isOpen ? "收起" : "展开"}${nodeTitle(n)}`}
                onClick={() =>
                  setExpanded((old) => {
                    const next = new Set(old);
                    if (next.has(n.id)) next.delete(n.id);
                    else next.add(n.id);
                    return next;
                  })
                }
              >
                <ChevronRight size={12} className={isOpen ? "rotated" : ""} />
              </button>
            ) : (
              <span className="tree-spacer" />
            )}
            <button onClick={() => inspect(n)}>
              {n.kind === "directory" ? (
                <Folder size={13} />
              ) : (
                <FileText size={13} />
              )}
              <span>{nodeTitle(n)}</span>
            </button>
          </div>
          {isOpen && branch(children, depth + 1)}
        </div>
      );
    });
  }
  return (
    <div className="context-tree">
      {branch(
        nodes.filter((n) => !n.parent_uri || !uris.has(n.parent_uri)),
        0,
      )}
    </div>
  );
}
