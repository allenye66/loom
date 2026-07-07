import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import './index.css';
import 'highlight.js/styles/github-dark.css';

const queryClient = new QueryClient({
  // Global poll cadence for queries without their own `refetchInterval`. The two heaviest
  // endpoints (/api/tasks, /api/chats) inherit this, and their work runs on the same process
  // that drives the terminal PTY — so an aggressive default showed up as typing/switch lag.
  // 5s is plenty fresh for task/chat lists; the live terminal has its own WS + faster pollers.
  defaultOptions: { queries: { refetchInterval: 5000, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
