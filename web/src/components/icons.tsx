import type { ReactNode, SVGProps } from "react";

/** Hand-drawn 24x24 stroke icons (no icon pack). They inherit color and scale with font size. */
type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Icon({ size = 16, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className="icon"
      {...rest}
    >
      {children}
    </svg>
  );
}

const make = (paths: ReactNode) => (props: IconProps) => <Icon {...props}>{paths}</Icon>;

export const LogoMark = (props: IconProps) => (
  <Icon {...props} strokeWidth={2}>
    <path d="M4 7h6l4 10h6" />
    <path d="M4 17h6l1.6-4" />
    <path d="M14 7h6" />
    <circle cx="4" cy="7" r="0.6" fill="currentColor" />
    <circle cx="4" cy="17" r="0.6" fill="currentColor" />
  </Icon>
);

export const IconTerminal = make(
  <>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <path d="m7 9 3 3-3 3M13 15h4" />
  </>,
);
export const IconChart = make(
  <>
    <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
  </>,
);
export const IconSun = make(
  <>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </>,
);
export const IconMoon = make(<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z" />);
export const IconMonitor = make(
  <>
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M8 20h8M12 16v4" />
  </>,
);
export const IconKey = make(
  <>
    <circle cx="8" cy="15" r="4" />
    <path d="m11 12 9-9M17 6l3 3M15 8l2 2" />
  </>,
);
export const IconSparkles = make(
  <>
    <path d="M12 3l1.8 4.6L18 9.5l-4.2 1.9L12 16l-1.8-4.6L6 9.5l4.2-1.9L12 3Z" />
    <path d="M19 15l.7 1.8 1.8.7-1.8.7L19 20l-.7-1.8-1.8-.7 1.8-.7L19 15Z" />
  </>,
);
export const IconSend = make(<path d="M4 12 20 4l-6 16-3-7-7-1Z" />);
export const IconCopy = make(
  <>
    <rect x="8" y="8" width="12" height="12" rx="2" />
    <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
  </>,
);
export const IconCheck = make(<path d="m5 12.5 4.5 4.5L19 7.5" />);
export const IconEye = make(
  <>
    <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z" />
    <circle cx="12" cy="12" r="3" />
  </>,
);
export const IconEyeOff = make(
  <>
    <path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-2.6 3.4M6.6 6.6C3.6 8.4 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6" />
    <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2M3 3l18 18" />
  </>,
);
export const IconAlert = make(
  <>
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v4M12 17h.01" />
  </>,
);
export const IconInfo = make(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </>,
);
export const IconX = make(<path d="M6 6l12 12M18 6 6 18" />);
export const IconZap = make(<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />);
export const IconDatabase = make(
  <>
    <ellipse cx="12" cy="5" rx="8" ry="3" />
    <path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" />
  </>,
);
export const IconClock = make(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </>,
);
export const IconCoins = make(
  <>
    <circle cx="9" cy="9" r="6" />
    <path d="M15.5 9.2A6 6 0 1 1 9.2 15.5" />
  </>,
);
export const IconRoute = make(
  <>
    <circle cx="6" cy="19" r="2" />
    <circle cx="18" cy="5" r="2" />
    <path d="M8 19h8.5a3.5 3.5 0 0 0 0-7h-9a3.5 3.5 0 0 1 0-7H16" />
  </>,
);
export const IconServer = make(
  <>
    <rect x="3" y="4" width="18" height="7" rx="2" />
    <rect x="3" y="13" width="18" height="7" rx="2" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </>,
);
export const IconShuffle = make(
  <path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5" />,
);
export const IconGauge = make(
  <>
    <path d="M12 14l4-4" />
    <path d="M3.3 19a10 10 0 1 1 17.4 0" />
  </>,
);
export const IconHash = make(<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18" />);
export const IconLogOut = make(
  <>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" />
  </>,
);
export const IconPower = make(
  <>
    <path d="M12 2v10" />
    <path d="M18.4 6.6a9 9 0 1 1-12.8 0" />
  </>,
);
export const IconRefresh = make(
  <>
    <path d="M21 12a9 9 0 0 1-15.5 6.2L3 16M3 12a9 9 0 0 1 15.5-6.2L21 8" />
    <path d="M21 3v5h-5M3 21v-5h5" />
  </>,
);
export const IconLock = make(
  <>
    <rect x="4" y="11" width="16" height="10" rx="2" />
    <path d="M8 11V7a4 4 0 0 1 8 0v4" />
  </>,
);
export const IconFlask = make(
  <>
    <path d="M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3" />
    <path d="M7 15h10" />
  </>,
);
export const IconPlus = make(<path d="M12 5v14M5 12h14" />);
export const IconArrowRight = make(<path d="M5 12h14M13 6l6 6-6 6" />);
export const IconActivity = make(<path d="M22 12h-4l-3 9L9 3l-3 9H2" />);
export const IconLayers = make(
  <>
    <path d="m12 2 10 5-10 5L2 7l10-5Z" />
    <path d="m2 17 10 5 10-5M2 12l10 5 10-5" />
  </>,
);
export const IconHistory = make(
  <>
    <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
    <path d="M3 3v5h5M12 7v5l3 2" />
  </>,
);
export const IconSearch = make(
  <>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </>,
);
export const IconHome = make(
  <>
    <path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1v-9.5Z" />
  </>,
);
export const IconColumns = make(
  <>
    <rect x="3" y="4" width="18" height="16" rx="2.5" />
    <path d="M12 4v16" />
  </>,
);
export const IconCommand = make(
  <path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6Z" />,
);
export const IconCpu = make(
  <>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <path d="M10 10h4v4h-4zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4" />
  </>,
);
export const IconShield = make(<path d="M12 3 4 6v6c0 4.5 3.4 8.2 8 9 4.6-.8 8-4.5 8-9V6l-8-3Z" />);
export const IconBox = make(
  <>
    <path d="m21 8-9-5-9 5 9 5 9-5Z" />
    <path d="M3 8v8l9 5 9-5V8M12 13v8" />
  </>,
);
export const IconTrophy = make(
  <>
    <path d="M8 4h8v5a4 4 0 0 1-8 0V4Z" />
    <path d="M8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 13v4M8 21h8M9 17h6" />
  </>,
);
