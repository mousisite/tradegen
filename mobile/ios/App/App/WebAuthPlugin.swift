import Foundation
import UIKit
import AuthenticationServices
import Capacitor

/// Opens a page in Apple's sign-in sheet (ASWebAuthenticationSession) and
/// hands back the address the page finally sends the sheet to.
///
/// Google refuses to sign anyone in inside an app's own web view, and the
/// Safari app would sign the person in to Safari rather than to the app. The
/// sheet is Apple's answer to both: it is a real browser, and it gives the
/// final address only to the app that opened it.
///
/// From the page:
///   Capacitor.nativePromise('WebAuth', 'start',
///       { url: 'https://orenth.app/auth/...', callbackScheme: 'app.orenth.ios',
///         ephemeral: false })
/// resolves to { url: 'app.orenth.ios://...' }, and rejects with the code
/// "CANCELLED" when the person closes the sheet.
///
/// The sheet says "Orenth wants to use orenth.app to sign in", and whatever
/// it ends on goes to the page that asked. So only a page of orenth.app may
/// ask (NOT_ALLOWED otherwise), and only for a sign-in address of the site,
/// https://orenth.app/auth/..., ending on the app's own scheme (INVALID_URL,
/// INVALID_SCHEME otherwise).
///
/// Registered by MainViewController.capacitorDidLoad().
@objc(WebAuthPlugin)
public class WebAuthPlugin: CAPPlugin, CAPBridgedPlugin, ASWebAuthenticationPresentationContextProviding {
    public let identifier: String = "WebAuthPlugin"
    public let jsName: String = "WebAuth"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise)
    ]

    // The sheet closes itself as soon as nothing holds it, so it is kept here
    // until it reports back. Only touched on the main thread.
    private var session: ASWebAuthenticationSession?

    @objc func start(_ call: CAPPluginCall) {
        guard let address: String = call.getString("url"),
              let url: URL = URL(string: address),
              OrenthSite.isSignInAddress(url) else {
            call.reject("Sign-in must start at https://orenth.app/auth/.", "INVALID_URL")
            return
        }
        guard let callbackScheme: String = call.getString("callbackScheme"),
              callbackScheme == OrenthSite.appScheme else {
            call.reject("The sign-in must end on the app's own address.", "INVALID_SCHEME")
            return
        }
        let ephemeral: Bool = call.getBool("ephemeral", false)

        // Plugin methods run on Capacitor's own queue; the web view and the
        // sheet are UI.
        DispatchQueue.main.async { [weak self] in
            guard let plugin: WebAuthPlugin = self else {
                call.reject("The sign-in sheet could not be opened.", "UNAVAILABLE")
                return
            }
            guard OrenthSite.showsSitePage(plugin.webView) else {
                call.reject("Only Orenth's own pages can open the sign-in sheet.", "NOT_ALLOWED")
                return
            }
            plugin.beginSession(call: call, url: url, callbackScheme: callbackScheme, ephemeral: ephemeral)
        }
    }

    private func beginSession(call: CAPPluginCall, url: URL, callbackScheme: String, ephemeral: Bool) {
        if self.session != nil {
            call.reject("A sign-in sheet is already open.", "BUSY")
            return
        }

        let authSession: ASWebAuthenticationSession = ASWebAuthenticationSession(
            url: url,
            callbackURLScheme: callbackScheme,
            completionHandler: { [weak self] (callbackURL: URL?, error: Error?) in
                let plugin: WebAuthPlugin? = self
                DispatchQueue.main.async {
                    plugin?.session = nil
                    WebAuthPlugin.finish(call: call, callbackURL: callbackURL, error: error)
                }
            })
        authSession.presentationContextProvider = self
        authSession.prefersEphemeralWebBrowserSession = ephemeral

        self.session = authSession
        let started: Bool = authSession.start()
        if !started {
            self.session = nil
            call.reject("The sign-in sheet could not be opened.", "START_FAILED")
        }
    }

    private static func finish(call: CAPPluginCall, callbackURL: URL?, error: Error?) {
        if let failure: Error = error {
            let nsError: NSError = failure as NSError
            if nsError.domain == ASWebAuthenticationSessionErrorDomain
                && nsError.code == ASWebAuthenticationSessionError.Code.canceledLogin.rawValue {
                call.reject("Sign-in was cancelled.", "CANCELLED")
            } else {
                call.reject(failure.localizedDescription, "FAILED", failure)
            }
            return
        }
        guard let finalURL: URL = callbackURL else {
            call.reject("The sign-in sheet returned no address.", "FAILED")
            return
        }
        call.resolve(["url": finalURL.absoluteString])
    }

    // MARK: - ASWebAuthenticationPresentationContextProviding

    // Called by the sheet on the main thread: the window to show it over.
    public func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        if let window: UIWindow = self.bridge?.viewController?.view.window {
            return window
        }
        if let window: UIWindow = self.bridge?.webView?.window {
            return window
        }
        for scene in UIApplication.shared.connectedScenes {
            if let windowScene: UIWindowScene = scene as? UIWindowScene {
                for window in windowScene.windows where window.isKeyWindow {
                    return window
                }
            }
        }
        return ASPresentationAnchor()
    }
}
