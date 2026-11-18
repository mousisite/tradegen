import Foundation
import UIKit
import StoreKit
import Capacitor

/// Apple's in-app purchase (StoreKit 2) for the pages the app shows: how Pro
/// is sold inside the app.
///
/// Nothing is unlocked here. Every purchase carries the signed-in account's
/// appAccountToken, and every transaction is handed to the page as Apple's
/// signed JWS (jwsRepresentation). The page sends it to orenth.app, which
/// checks Apple's signature and decides who has Pro, and only then does the
/// page finish the transaction. Until it is finished, StoreKit keeps
/// offering it, so nothing is lost if the page closes or the network drops.
///
/// From the page (static/app-purchase.js), with no bundler:
///   Capacitor.nativePromise('AppPurchase', 'products', { ids: [...] })
///       -> { products: [{ id, title, description, displayPrice, price,
///            currencyCode, periodUnit, periodValue, ... }] }
///   'purchase', { productId, appAccountToken }  -> one transaction, below
///   'entitlements'                     -> { jws: [...], transactions: [...] }
///   'restore'   (Restore Purchases tap only)  -> { jws: [...], transactions: [...] }
///   'unfinished'                       -> { jws: [...], transactions: [...] }
///   'finish', { transactionId }        -> { finished: true or false }
///   'manageSubscriptions'              -> {}  (Apple's own sheet)
///   'storefront'                       -> { countryCode, id }
///   Capacitor.addListener('AppPurchase', 'transaction', fn)
/// A transaction is { jws, verified, transactionId, originalTransactionId,
/// productId, purchaseDate, expiresDate?, appAccountToken?, revoked? }.
/// Failures carry a code: CANCELLED, PENDING (Ask to Buy), BUSY, NOT_FOUND,
/// BAD_INPUT, STORE_UNAVAILABLE, PURCHASE_FAILED, RESTORE_FAILED, NO_SCENE,
/// MANAGE_FAILED.
///
/// StoreKit 2 needs iOS 15, which the app already requires. On iOS 17 and
/// later the purchase sheet is shown over the app's own window, as Apple
/// asks of UIKit apps (purchase(confirmIn:)).
///
/// Registered by MainViewController.capacitorDidLoad(), next to the sign-in
/// sheet. The App target needs the In-App Purchase capability.
@objc(AppPurchasePlugin)
public class AppPurchasePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier: String = "AppPurchasePlugin"
    public let jsName: String = "AppPurchase"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "products", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "purchase", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "entitlements", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "restore", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "unfinished", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "finish", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "manageSubscriptions", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "storefront", returnType: CAPPluginReturnPromise)
    ]

    private var updatesTask: Task<Void, Never>?
    // Only read and written on the main actor.
    private var purchasing: Bool = false

    override public func load() {
        // Apple: listen from launch, or transactions that happen outside a
        // purchase (renewals, Ask to Buy approvals, offer codes, purchases on
        // other devices, and unfinished ones from last time, which arrive
        // once, right after launch) can be missed. No page is loaded yet at
        // launch, and Capacitor drops every listener when a page loads, so
        // each event is kept until a page listens for it.
        updatesTask = Task.detached(priority: .background) { [weak self] in
            for await result in Transaction.updates {
                guard let plugin: AppPurchasePlugin = self else {
                    return
                }
                let data: [String: Any] = AppPurchasePlugin.describe(result)
                await MainActor.run {
                    plugin.notifyListeners("transaction", data: data, retainUntilConsumed: true)
                }
            }
        }
    }

    deinit {
        updatesTask?.cancel()
    }

    // MARK: - Products

    @objc func products(_ call: CAPPluginCall) {
        guard let ids: [String] = call.getArray("ids", String.self),
              !ids.isEmpty, ids.count <= 20,
              ids.allSatisfy({ (id: String) -> Bool in !id.isEmpty && id.count <= 100 }) else {
            call.reject("ids must list 1 to 20 product ids", "BAD_INPUT")
            return
        }
        Task {
            do {
                let found: [Product] = try await Product.products(for: ids)
                var list: [[String: Any]] = []
                for product in found {
                    list.append(await AppPurchasePlugin.describe(product))
                }
                call.resolve(["products": list])
            } catch {
                call.reject(error.localizedDescription, "STORE_UNAVAILABLE")
            }
        }
    }

    // MARK: - Purchase

    @objc func purchase(_ call: CAPPluginCall) {
        guard let productId: String = call.getString("productId"), !productId.isEmpty else {
            call.reject("productId is required", "BAD_INPUT")
            return
        }
        // The server makes this token for the signed-in account. Without a
        // valid one the purchase could not be matched to an account, so stop
        // here rather than buy without it.
        guard let tokenText: String = call.getString("appAccountToken"),
              let token: UUID = UUID(uuidString: tokenText) else {
            call.reject("appAccountToken must be a UUID", "BAD_INPUT")
            return
        }
        Task { @MainActor in
            if self.purchasing {
                call.reject("A purchase is already in progress", "BUSY")
                return
            }
            self.purchasing = true
            defer { self.purchasing = false }
            do {
                let found: [Product] = try await Product.products(for: [productId])
                guard let product: Product = found.first else {
                    call.reject("This product is not available", "NOT_FOUND")
                    return
                }
                let options: Set<Product.PurchaseOption> = [.appAccountToken(token)]
                let result: Product.PurchaseResult
                if #available(iOS 17.0, *),
                   let scene: UIWindowScene = self.bridge?.viewController?.viewIfLoaded?.window?.windowScene {
                    result = try await product.purchase(confirmIn: scene, options: options)
                } else {
                    result = try await product.purchase(options: options)
                }
                switch result {
                case .success(let verification):
                    // Not finished here. The page finishes it once the server
                    // has stored it; until then StoreKit keeps offering it.
                    call.resolve(AppPurchasePlugin.describe(verification))
                case .pending:
                    // Ask to Buy or a bank check. The outcome arrives later as
                    // a "transaction" event.
                    call.reject("The purchase is waiting for approval", "PENDING")
                case .userCancelled:
                    call.reject("The purchase was cancelled", "CANCELLED")
                @unknown default:
                    call.reject("The App Store gave an unknown answer", "UNKNOWN")
                }
            } catch StoreKitError.userCancelled {
                call.reject("The purchase was cancelled", "CANCELLED")
            } catch {
                call.reject(error.localizedDescription, "PURCHASE_FAILED")
            }
        }
    }

    // MARK: - Entitlements, restore, unfinished, finish

    @objc func entitlements(_ call: CAPPluginCall) {
        Task {
            let items: [[String: Any]] = await AppPurchasePlugin.collect(Transaction.currentEntitlements)
            call.resolve(AppPurchasePlugin.listing(items))
        }
    }

    @objc func restore(_ call: CAPPluginCall) {
        Task {
            do {
                // Asks for the Apple Account's password, so the page calls it
                // only from a tap on Restore Purchases.
                try await AppStore.sync()
            } catch StoreKitError.userCancelled {
                call.reject("Restoring was cancelled", "CANCELLED")
                return
            } catch {
                call.reject(error.localizedDescription, "RESTORE_FAILED")
                return
            }
            let items: [[String: Any]] = await AppPurchasePlugin.collect(Transaction.currentEntitlements)
            call.resolve(AppPurchasePlugin.listing(items))
        }
    }

    @objc func unfinished(_ call: CAPPluginCall) {
        Task {
            let items: [[String: Any]] = await AppPurchasePlugin.collect(Transaction.unfinished)
            call.resolve(AppPurchasePlugin.listing(items))
        }
    }

    @objc func finish(_ call: CAPPluginCall) {
        guard let text: String = call.getString("transactionId"), let id: UInt64 = UInt64(text) else {
            call.reject("transactionId is required", "BAD_INPUT")
            return
        }
        Task {
            var finished: Bool = false
            for await result in Transaction.unfinished {
                let transaction: Transaction = AppPurchasePlugin.unwrap(result)
                if transaction.id == id {
                    await transaction.finish()
                    finished = true
                    break
                }
            }
            call.resolve(["finished": finished])
        }
    }

    // MARK: - Manage subscriptions, storefront

    @objc func manageSubscriptions(_ call: CAPPluginCall) {
        Task { @MainActor in
            guard let scene: UIWindowScene = self.bridge?.viewController?.viewIfLoaded?.window?.windowScene else {
                call.reject("There is no window to show the sheet in", "NO_SCENE")
                return
            }
            do {
                try await AppStore.showManageSubscriptions(in: scene)
                call.resolve()
            } catch {
                call.reject(error.localizedDescription, "MANAGE_FAILED")
            }
        }
    }

    @objc func storefront(_ call: CAPPluginCall) {
        Task {
            let current: Storefront? = await Storefront.current
            call.resolve(["countryCode": current?.countryCode ?? "", "id": current?.id ?? ""])
        }
    }

    // MARK: - Helpers

    private static func collect(_ sequence: Transaction.Transactions) async -> [[String: Any]] {
        var items: [[String: Any]] = []
        for await result in sequence {
            items.append(describe(result))
        }
        return items
    }

    private static func listing(_ items: [[String: Any]]) -> [String: Any] {
        return ["transactions": items, "jws": items.compactMap { (item: [String: Any]) -> String? in item["jws"] as? String }]
    }

    private static func unwrap(_ result: VerificationResult<Transaction>) -> Transaction {
        switch result {
        case .verified(let transaction):
            return transaction
        case .unverified(let transaction, _):
            return transaction
        }
    }

    /// What the page needs: the JWS for the server, and a few fields for the
    /// screen. "verified" is StoreKit's own check; the server checks again.
    private static func describe(_ result: VerificationResult<Transaction>) -> [String: Any] {
        let transaction: Transaction
        let verified: Bool
        switch result {
        case .verified(let value):
            transaction = value
            verified = true
        case .unverified(let value, _):
            transaction = value
            verified = false
        }
        var data: [String: Any] = [
            "jws": result.jwsRepresentation,
            "verified": verified,
            "transactionId": String(transaction.id),
            "originalTransactionId": String(transaction.originalID),
            "productId": transaction.productID,
            "purchaseDate": milliseconds(transaction.purchaseDate)
        ]
        if let token: UUID = transaction.appAccountToken {
            data["appAccountToken"] = token.uuidString.lowercased()
        }
        if let expires: Date = transaction.expirationDate {
            data["expiresDate"] = milliseconds(expires)
        }
        if transaction.revocationDate != nil {
            data["revoked"] = true
        }
        return data
    }

    /// Name, length and price exactly as the App Store gives them, in the
    /// buyer's own currency, for the subscribe screen.
    private static func describe(_ product: Product) async -> [String: Any] {
        var data: [String: Any] = [
            "id": product.id,
            "title": product.displayName,
            "description": product.description,
            "displayPrice": product.displayPrice,
            "price": "\(product.price)",
            "currencyCode": product.priceFormatStyle.currencyCode
        ]
        if let subscription: Product.SubscriptionInfo = product.subscription {
            data["periodUnit"] = unitName(subscription.subscriptionPeriod.unit)
            data["periodValue"] = subscription.subscriptionPeriod.value
            data["groupId"] = subscription.subscriptionGroupID
            if let offer: Product.SubscriptionOffer = subscription.introductoryOffer {
                let intro: [String: Any] = [
                    "displayPrice": offer.displayPrice,
                    "paymentMode": paymentModeName(offer.paymentMode),
                    "periodUnit": unitName(offer.period.unit),
                    "periodValue": offer.period.value,
                    "periodCount": offer.periodCount
                ]
                data["introOffer"] = intro
                data["introEligible"] = await subscription.isEligibleForIntroOffer
            }
        }
        return data
    }

    private static func unitName(_ unit: Product.SubscriptionPeriod.Unit) -> String {
        switch unit {
        case .day: return "day"
        case .week: return "week"
        case .month: return "month"
        case .year: return "year"
        @unknown default: return "unknown"
        }
    }

    // PaymentMode is a struct with static values, not an enum, so compare.
    private static func paymentModeName(_ mode: Product.SubscriptionOffer.PaymentMode) -> String {
        if mode == .freeTrial { return "freeTrial" }
        if mode == .payAsYouGo { return "payAsYouGo" }
        if mode == .payUpFront { return "payUpFront" }
        return "other"
    }

    private static func milliseconds(_ date: Date) -> Int {
        return Int((date.timeIntervalSince1970 * 1000).rounded())
    }
}
