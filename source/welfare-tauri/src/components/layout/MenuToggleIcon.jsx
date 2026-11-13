export default function MenuToggleIcon({open}) {
    return <span aria-hidden="true" data-menu-toggle={open ? 'open' : 'closed'} className="relative block h-5 w-5 shrink-0">
        <span className={`absolute left-0 top-[3px] h-0.5 w-5 rounded-full bg-current transition-transform duration-200 ease-in-out motion-reduce:transition-none ${open?'translate-y-1.5 rotate-45':''}`}/>
        <span className={`absolute left-0 top-[9px] h-0.5 w-5 rounded-full bg-current transition-opacity duration-200 ease-in-out motion-reduce:transition-none ${open?'opacity-0':''}`}/>
        <span className={`absolute left-0 top-[15px] h-0.5 w-5 rounded-full bg-current transition-transform duration-200 ease-in-out motion-reduce:transition-none ${open?'-translate-y-1.5 -rotate-45':''}`}/>
    </span>;
}
