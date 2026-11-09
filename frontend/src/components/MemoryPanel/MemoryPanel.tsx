import { useEffect, useState, type FormEvent } from 'react';
import type { ApiClient } from '../../api/client';
import { describeError } from '../../api/errors';
import controls from '../../styles/controls.module.css';
import styles from './MemoryPanel.module.css';

export function MemoryPanel({ client }: { client: ApiClient }) {
  const [text, setText] = useState('');
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    if (!client.getMemory) return;
    void client
      .getMemory()
      .then((user) => {
        if (active) {
          setText(user.instructions ?? '');
          setReady(true);
        }
      })
      .catch((error) => {
        if (active) setNotice(describeError(error));
      });
    return () => {
      active = false;
    };
  }, [client]);
  if (!client.getMemory || !client.saveMemory) return null;
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!client.saveMemory) return;
    setSaving(true);
    setNotice('');
    try {
      await client.saveMemory(text);
      setNotice('Saved. Applies to your next request.');
    } catch (error) {
      setNotice(describeError(error));
    } finally {
      setSaving(false);
    }
  };
  return (
    <details className={styles.panel}>
      <summary>Standing instructions</summary>
      <form onSubmit={(event) => void save(event)}>
        <label htmlFor="memory-instructions">Instructions to use across chats</label>
        <textarea
          id="memory-instructions"
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={2000}
          disabled={!ready || saving}
        />
        <button className={controls.button} disabled={!ready || saving}>
          Save instructions
        </button>
        {notice && <p role="status">{notice}</p>}
      </form>
    </details>
  );
}
