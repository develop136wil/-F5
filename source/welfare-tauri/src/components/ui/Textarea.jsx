import * as React from "react"
import { cn } from "../../lib/utils"

const Textarea = React.forwardRef(({ className, ...props }, ref) => {
    return (
        <textarea
            className={cn(
                "flex min-h-[80px] w-full rounded-2xl bg-gray-50 dark:bg-gray-800 px-4 py-3 text-[15px] font-medium transition-all outline-none ring-0 focus:bg-white dark:focus:bg-gray-900 focus:shadow-[0_0_0_2px_rgba(49,130,246,0.3)] placeholder:text-gray-400 disabled:cursor-not-allowed disabled:opacity-50",
                className
            )}
            ref={ref}
            {...props}
        />
    )
})
Textarea.displayName = "Textarea"

export { Textarea }
