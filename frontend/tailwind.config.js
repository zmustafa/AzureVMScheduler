/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: { extend: { colors: { ink: '#f8fafc', panel: '#ffffff', azure: '#0078d4' }, boxShadow: { glow: '0 10px 30px rgba(15,23,42,.07)' } } },
  plugins: [],
}
