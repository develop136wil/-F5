import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from './context/ThemeContext'
import { ScrapeProvider } from './context/ScrapeContext'
import App from './App.jsx'
import DesktopLifecycle from './components/DesktopLifecycle'
import './index.css'
import { MotionConfig } from 'framer-motion'

const queryClient = new QueryClient()

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <MotionConfig reducedMotion="user">
        <QueryClientProvider client={queryClient}>
            <ThemeProvider>
                <DesktopLifecycle>
                <ScrapeProvider>
                    <BrowserRouter>
                        <App />
                    </BrowserRouter>
                </ScrapeProvider>
                </DesktopLifecycle>
            </ThemeProvider>
        </QueryClientProvider>
        </MotionConfig>
    </React.StrictMode>,
)
