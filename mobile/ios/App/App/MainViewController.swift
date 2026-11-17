import UIKit
import WebKit
import Capacitor

/// The app's one screen: Capacitor's web view showing orenth.app, plus the
/// few things a web page cannot do for itself.
///
/// - Registers WebAuthPlugin, the sign-in sheet (see WebAuthPlugin.swift).
/// - Swiping in from the left edge goes back a page, as in Safari.
/// - Pulling the page down reloads it, and the spinner stops when the page
///   has finished loading.
/// - Before the first page arrives, the screen is the site's own ground,
///   light or dark to match the phone.
///
/// SceneDelegate.swift creates this screen (Capacitor 8.5 and later build the
/// window in code), and Main.storyboard names it too.
class MainViewController: CAPBridgeViewController {
    private var refresher: UIRefreshControl?
    // Ends the observation by itself when this screen goes away.
    private var loadingObservation: NSKeyValueObservation?

    // Called once, after Capacitor has made the web view and the bridge and
    // before the first page loads.
    override func capacitorDidLoad() {
        super.capacitorDidLoad()
        bridge?.registerPluginInstance(WebAuthPlugin())

        guard let webView: WKWebView = self.webView else {
            return
        }
        webView.allowsBackForwardNavigationGestures = true

        // The site's paper colour, or its dark ground in dark mode.
        let ground: UIColor = UIColor(dynamicProvider: { (traits: UITraitCollection) -> UIColor in
            if traits.userInterfaceStyle == .dark {
                return UIColor(red: 25.0 / 255.0, green: 24.0 / 255.0, blue: 23.0 / 255.0, alpha: 1.0)
            }
            return UIColor(red: 244.0 / 255.0, green: 243.0 / 255.0, blue: 238.0 / 255.0, alpha: 1.0)
        })
        webView.backgroundColor = ground
        webView.scrollView.backgroundColor = ground

        // Capacitor turns the bounce off; a pull to refresh needs it.
        let scrollView: UIScrollView = webView.scrollView
        scrollView.bounces = true
        scrollView.alwaysBounceVertical = true

        let control: UIRefreshControl = UIRefreshControl()
        control.addTarget(self, action: #selector(pulledToRefresh(_:)), for: .valueChanged)
        scrollView.refreshControl = control
        refresher = control

        // The spinner stops when the page stops loading, whether it loaded,
        // failed, or was replaced by the offline page.
        loadingObservation = webView.observe(\.isLoading, options: [.new]) { [weak self] (observed: WKWebView, change: NSKeyValueObservedChange<Bool>) in
            let stillLoading: Bool = change.newValue ?? false
            if stillLoading {
                return
            }
            guard let screen: MainViewController = self else {
                return
            }
            DispatchQueue.main.async {
                screen.stopRefreshing()
            }
        }
    }

    @objc private func pulledToRefresh(_ sender: UIRefreshControl) {
        guard let webView: WKWebView = self.webView else {
            sender.endRefreshing()
            return
        }
        // On the offline page, "again" means the site, not that page.
        let server: URL? = bridge?.config.serverURL
        if let current: URL = webView.url,
           let home: URL = server,
           current.scheme == home.scheme,
           current.host == home.host {
            _ = webView.reload()
        } else if let home: URL = server {
            _ = webView.load(URLRequest(url: home))
        } else {
            _ = webView.reload()
        }
    }

    private func stopRefreshing() {
        if let control: UIRefreshControl = refresher, control.isRefreshing {
            control.endRefreshing()
        }
    }
}
