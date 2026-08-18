import type { Metadata } from "next";
import "./globals.css";
export const metadata:Metadata={title:"Estoque Fácil",description:"Controle simples de produtos e movimentações de estoque.",manifest:"/manifest.webmanifest"};
export default function RootLayout({children}:Readonly<{children:React.ReactNode}>){return <html lang="pt-BR"><body>{children}</body></html>}
