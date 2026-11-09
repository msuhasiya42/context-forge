import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createApiClient } from './api/client';
import { App } from './App';
import './styles/tokens.css';
import './styles/global.css';

const root = document.getElementById('root');
if (!root) throw new Error('Missing #root element');

const client = createApiClient(import.meta.env.VITE_API_BASE_URL);

createRoot(root).render(
  <StrictMode>
    <App client={client} />
  </StrictMode>,
);
