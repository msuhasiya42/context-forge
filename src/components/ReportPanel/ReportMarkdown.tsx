import Markdown, { type Components } from 'react-markdown';

// The panel's own title is the h2, so the report's headings start one level below it.
const components: Components = {
  h1: ({ children }) => <h3>{children}</h3>,
  h2: ({ children }) => <h4>{children}</h4>,
  h3: ({ children }) => <h5>{children}</h5>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

/**
 * A report's markdown as React elements. The text quotes web pages, so raw HTML in it is
 * dropped (skipHtml) and react-markdown's default URL filter removes javascript: and other
 * unsafe link targets.
 */
export function ReportMarkdown({ text }: { text: string }) {
  return (
    <Markdown skipHtml components={components}>
      {text}
    </Markdown>
  );
}
